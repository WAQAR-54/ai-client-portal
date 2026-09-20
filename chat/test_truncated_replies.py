"""A reply the provider cut short must be flagged, not presented as a finished answer.

Regression for: a Gemini stream that ended without a finish reason (observed on a real call:
857 characters, no finishReason, the text stops inside a URL) was saved and shown as a
complete reply - and cached, so the same question returned the same broken answer."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import User
from chat.models import Conversation, Message
from chat.providers import AnthropicProvider, GeminiProvider, OpenAICompatibleProvider, StreamChunk
from chat.tests import _grant_premium_plan
from providers.models import Provider, ProviderModel


def _done(chunks):
    return next(chunk for chunk in chunks if chunk.done)


class OpenAIFinishReasonTests(SimpleTestCase):
    def _stream(self, finish_reason):
        def piece(text=None, finish=None, usage=None):
            choice = SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish)
            return SimpleNamespace(choices=[choice], usage=usage)

        usage = SimpleNamespace(prompt_tokens=5, completion_tokens=7)
        chunks = [piece("Hello "), piece("wor"), piece(None, finish_reason), SimpleNamespace(choices=[], usage=usage)]
        provider = OpenAICompatibleProvider(SimpleNamespace(name="OpenAI", get_decrypted_key=lambda: "k", base_url=""))
        client = MagicMock()
        client.chat.completions.create.return_value = iter(chunks)
        with patch.object(OpenAICompatibleProvider, "_client", return_value=client):
            return list(provider.stream_chat([{"role": "user", "content": "hi"}], "m"))

    def test_a_reply_that_hit_the_length_limit_is_truncated(self):
        self.assertTrue(_done(self._stream("length")).truncated)

    def test_a_content_filter_stop_is_truncated(self):
        self.assertTrue(_done(self._stream("content_filter")).truncated)

    def test_a_normal_stop_is_not_truncated(self):
        self.assertFalse(_done(self._stream("stop")).truncated)

    def test_a_provider_that_never_sends_a_finish_reason_is_not_guessed_at(self):
        """Only Gemini's stream is known to end without one when it is incomplete."""
        self.assertFalse(_done(self._stream(None)).truncated)


class AnthropicStopReasonTests(SimpleTestCase):
    def _stream(self, stop_reason):
        final = SimpleNamespace(usage=SimpleNamespace(input_tokens=3, output_tokens=4), stop_reason=stop_reason)
        stream = MagicMock()
        stream.text_stream = iter(["Hi ", "there"])
        stream.get_final_message.return_value = final
        manager = MagicMock()
        manager.__enter__.return_value = stream
        client = MagicMock()
        client.messages.stream.return_value = manager
        provider = AnthropicProvider(SimpleNamespace(name="Anthropic", get_decrypted_key=lambda: "k"))
        with patch.object(AnthropicProvider, "_client", return_value=client):
            return list(provider.stream_chat([{"role": "user", "content": "hi"}], "m"))

    def test_max_tokens_is_truncated(self):
        self.assertTrue(_done(self._stream("max_tokens")).truncated)

    def test_end_turn_is_not(self):
        self.assertFalse(_done(self._stream("end_turn")).truncated)


class GeminiFinishReasonTests(SimpleTestCase):
    def _stream(self, finish_reason):
        def line(text, finish=None):
            candidate = {"content": {"parts": [{"text": text}]}}
            if finish:
                candidate["finishReason"] = finish
            return "data: " + __import__("json").dumps({"candidates": [candidate]})

        lines = [line("Hello "), line("world", finish_reason)]
        response = MagicMock()
        response.iter_lines.return_value = lines
        provider = GeminiProvider(SimpleNamespace(name="Gemini", get_decrypted_key=lambda: "k"))
        with patch("chat.providers._RETRYING_SESSION.post", return_value=response):
            return list(provider.stream_chat([{"role": "user", "content": "hi"}], "m"))

    def test_stop_is_complete(self):
        self.assertFalse(_done(self._stream("STOP")).truncated)

    def test_max_tokens_and_safety_are_truncated(self):
        for reason in ("MAX_TOKENS", "SAFETY", "RECITATION"):
            self.assertTrue(_done(self._stream(reason)).truncated, reason)

    def test_a_stream_that_ends_with_no_finish_reason_is_truncated(self):
        self.assertTrue(_done(self._stream(None)).truncated)


class TruncatedReplyViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="trunc@example.com", password="pw12345!")
        self.model = ProviderModel.objects.create(
            provider=Provider.objects.get(slug="openai"),
            model_id="trunc-model",
            tier=ProviderModel.Tier.DEFAULT,
            input_price_per_mtok=1,
            output_price_per_mtok=2,
            is_enabled=True,
        )
        _grant_premium_plan(self.user, self.model)
        self.client.force_login(self.user)

    def _reply(self, chunks):
        conversation = Conversation.objects.create(user=self.user)
        Message.objects.create(conversation=conversation, role=Message.Role.USER, content="tell me")
        pending = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")
        url = reverse(
            "chat:stream_message",
            kwargs={"conversation_id": conversation.id, "message_id": pending.id, "token": pending.stream_token},
        )
        with patch("chat.views.classify_complexity", return_value=ProviderModel.Tier.DEFAULT):
            with patch("chat.views.get_provider") as provider:
                provider.return_value.stream_chat.side_effect = lambda *a, **k: iter(chunks)
                body = b"".join(self.client.get(url).streaming_content).decode()
        pending.refresh_from_db()
        return pending, body

    def test_a_truncated_reply_says_so_in_the_stream_and_in_what_is_saved(self):
        chunks = [StreamChunk(text="See https://exa"), StreamChunk(done=True, truncated=True)]
        message, body = self._reply(chunks)
        self.assertIn("cut off before it finished", message.content)
        self.assertTrue(message.content.startswith("See https://exa"))
        self.assertIn("cut off before it finished", body)

    def test_a_complete_reply_gets_no_notice(self):
        message, body = self._reply([StreamChunk(text="All done."), StreamChunk(done=True)])
        self.assertEqual(message.content, "All done.")
        self.assertNotIn("cut off", body)

    def test_a_truncated_reply_is_not_cached_but_a_complete_one_is(self):
        with patch("chat.views.store_cached_response") as store:
            self._reply([StreamChunk(text="half"), StreamChunk(done=True, truncated=True)])
            store.assert_not_called()
            self._reply([StreamChunk(text="whole"), StreamChunk(done=True)])
            store.assert_called_once()
