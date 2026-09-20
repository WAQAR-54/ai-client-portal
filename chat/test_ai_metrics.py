"""AI provider-call counters: real outcomes, no content, and never a reason for a reply to fail."""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from chat import ai_metrics
from chat.models import Conversation, Message
from chat.providers import ProviderError, StreamChunk
from chat.tests import _grant_premium_plan
from providers.models import Provider, ProviderModel


class CounterTests(TestCase):
    def setUp(self):
        cache.clear()
        self.model = ProviderModel.objects.create(
            provider=Provider.objects.get(slug="openai"), model_id="metric-model", is_enabled=True
        )

    def test_success_and_failure_counters_and_average_latency(self):
        ai_metrics.record_success("openai", "metric-model", 100)
        ai_metrics.record_success("openai", "metric-model", 300, truncated=True)
        ai_metrics.record_failure("openai", "metric-model", "rate_limited")
        ai_metrics.record_failure("openai", "metric-model", "timeout")
        ai_metrics.record_failure("openai", "metric-model", "unknown")
        totals = ai_metrics.read("openai", "metric-model")
        self.assertEqual(totals["requests"], 5)
        self.assertEqual((totals["success"], totals["failure"]), (2, 3))
        self.assertEqual((totals["rate_limited"], totals["timeout"], totals["truncated"]), (1, 1, 1))
        row = ai_metrics.summary()["rows"][0]
        self.assertEqual((row["provider"], row["model"], row["avg_latency_ms"]), ("openai", "metric-model", 200))

    def test_a_fallback_answer_is_counted_separately(self):
        ai_metrics.record_success("openai", "metric-model", 50, fallback=True)
        self.assertEqual(ai_metrics.read("openai", "metric-model")["fallback_success"], 1)

    def test_models_with_no_traffic_are_not_listed(self):
        self.assertEqual(ai_metrics.summary()["rows"], [])

    def test_only_slugs_model_ids_and_numbers_are_ever_stored(self):
        ai_metrics.record_success("openai", "metric-model", 10)
        ai_metrics.record_failure("openai", "metric-model", "authentication")
        keys = [k for k in cache._cache if "aimetrics" in k] if hasattr(cache, "_cache") else []
        for key in keys:
            self.assertRegex(key.split(":", 1)[-1], r"^[A-Za-z0-9_.:\-]+$")

    def test_an_unavailable_cache_is_silent(self):
        with patch.object(ai_metrics.cache, "incr", side_effect=ConnectionError("down")), patch.object(
            ai_metrics.cache, "add", side_effect=ConnectionError("down")
        ):
            ai_metrics.record_success("openai", "metric-model", 10)
            ai_metrics.record_failure("openai", "metric-model", "timeout")
        with patch.object(ai_metrics.cache, "get_many", side_effect=ConnectionError("down")):
            self.assertEqual(ai_metrics.read("openai", "metric-model")["requests"], 0)


class StreamRecordingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="metrics@example.com", password="pw12345!")
        openai = Provider.objects.get(slug="openai")
        self.cheap = ProviderModel.objects.create(
            provider=openai,
            model_id="cheap-first",
            tier=ProviderModel.Tier.DEFAULT,
            input_price_per_mtok=1,
            output_price_per_mtok=1,
            is_enabled=True,
        )
        self.dear = ProviderModel.objects.create(
            provider=openai,
            model_id="dear-second",
            tier=ProviderModel.Tier.DEFAULT,
            input_price_per_mtok=9,
            output_price_per_mtok=9,
            is_enabled=True,
        )
        _grant_premium_plan(self.user, self.cheap, self.dear)
        self.client.force_login(self.user)

    def stream(self, behaviour):
        conversation = Conversation.objects.create(user=self.user)
        Message.objects.create(conversation=conversation, role="user", content="hello there")
        pending = Message.objects.create(conversation=conversation, role="assistant", content="")
        url = reverse(
            "chat:stream_message",
            kwargs={"conversation_id": conversation.id, "message_id": pending.id, "token": pending.stream_token},
        )
        calls = iter(range(10))

        def fake(history, model_id, **kwargs):
            return behaviour(next(calls), model_id)

        with patch("chat.views.classify_complexity", return_value=ProviderModel.Tier.DEFAULT), patch(
            "chat.views.get_provider"
        ) as provider:
            provider.return_value.stream_chat.side_effect = fake
            body = b"".join(self.client.get(url).streaming_content).decode()
        pending.refresh_from_db()
        return pending, body

    def test_a_normal_reply_is_counted_with_its_latency(self):
        pending, _ = self.stream(lambda n, m: iter([StreamChunk(text="hi"), StreamChunk(done=True)]))
        totals = ai_metrics.read("openai", pending.provider_model_used.model_id)
        self.assertEqual((totals["requests"], totals["success"], totals["failure"]), (1, 1, 0))
        self.assertEqual(totals["fallback_success"], 0)

    def test_a_failed_first_model_and_a_fallback_answer_are_both_counted(self):
        def behaviour(n, model_id):
            if n == 0:
                raise ProviderError("Error code: 429 - rate limit exceeded for sk-abcdefghijklmnopqrstuvwx")
            return iter([StreamChunk(text="from the fallback"), StreamChunk(done=True)])

        pending, _ = self.stream(behaviour)
        self.assertEqual(pending.content, "from the fallback")
        first = ai_metrics.read("openai", "cheap-first")
        second = ai_metrics.read("openai", "dear-second")
        self.assertEqual((first["failure"], first["rate_limited"], first["success"]), (1, 1, 0))
        self.assertEqual((second["success"], second["fallback_success"]), (1, 1))

    def test_a_truncated_reply_is_counted(self):
        pending, _ = self.stream(lambda n, m: iter([StreamChunk(text="half"), StreamChunk(done=True, truncated=True)]))
        self.assertEqual(ai_metrics.read("openai", pending.provider_model_used.model_id)["truncated"], 1)

    def test_a_metrics_outage_never_breaks_the_reply(self):
        with patch.object(ai_metrics.cache, "incr", side_effect=ConnectionError("down")), patch.object(
            ai_metrics.cache, "add", side_effect=ConnectionError("down")
        ):
            pending, _ = self.stream(lambda n, m: iter([StreamChunk(text="still works"), StreamChunk(done=True)]))
        self.assertEqual(pending.content, "still works")

    def test_counters_hold_no_prompt_reply_or_key_material(self):
        def behaviour(n, model_id):
            raise ProviderError("Error code: 401 - bad key sk-abcdefghijklmnopqrstuvwx for prompt 'hello there'")

        self.stream(behaviour)
        stored = " ".join(str(k) for k in getattr(cache, "_cache", {}))
        for forbidden in ("sk-abc", "hello there", "bad key"):
            self.assertNotIn(forbidden, stored)
