"""What a chat request sends is bounded, and an attachment is read once, not on every turn.

Regression for: every reply loaded every message of the conversation, re-parsed every attachment
on every turn, and the only limit (a plan cap) rejected the request - or did not exist at all when
the plan had no cap."""

import shutil
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import User
from chat import context_window as cw
from chat.models import Conversation, Message
from chat.providers import StreamChunk
from chat.tests import _grant_premium_plan
from governance.models import Plan
from providers.models import Provider, ProviderModel


def turn(role, text, images=None):
    t = {"role": role, "content": text}
    if images:
        t["images"] = images
    return t


def conversation_turns(pairs, size=400):
    out = []
    for i in range(pairs):
        out.append(turn("user", f"question {i} " + "q" * size))
        out.append(turn("assistant", f"answer {i} " + "a" * size))
    out.append(turn("user", "the current question"))
    return out


class FitTests(SimpleTestCase):
    def test_everything_that_fits_is_sent_unchanged(self):
        turns = conversation_turns(2, size=20)
        fitted = cw.fit(cw.Window(turns=turns), "sys", 10_000)
        self.assertEqual(fitted.turns, turns)
        self.assertEqual((fitted.omitted, fitted.fits), (0, True))

    def test_oldest_turns_go_first_and_the_current_message_always_stays(self):
        turns = conversation_turns(10)
        budget = 600  # tokens: room for a few turns only
        fitted = cw.fit(cw.Window(turns=turns), "sys", budget)
        self.assertTrue(fitted.fits)
        self.assertLessEqual(fitted.tokens, budget)  # the notes block is inside the budget
        self.assertEqual(fitted.turns[-1]["content"], "the current question")
        self.assertGreater(fitted.omitted, 0)
        self.assertLess(len(fitted.turns), len(turns))
        # kept turns are the NEWEST ones, contiguous, ending at the current message
        kept_questions = [t["content"] for t in fitted.turns if t["content"].startswith("question")]
        self.assertTrue(all(f"question {i} " in " ".join(kept_questions) for i in (9,)))

    def test_a_provider_conversation_never_opens_with_a_reply(self):
        for budget in range(150, 1200, 37):
            fitted = cw.fit(cw.Window(turns=conversation_turns(8)), "sys", budget)
            self.assertEqual(fitted.turns[0]["role"], "user", budget)

    def test_dropped_turns_leave_the_brief_and_notes_in_the_first_kept_turn(self):
        window = cw.Window(
            turns=conversation_turns(6, size=20),
            older_count=30,
            brief="Help me plan the Q3 launch of Acme",
            older_notes=["what about pricing?", "and the timeline?"],
        )
        fitted = cw.fit(window, "sys", 400)
        head = fitted.turns[0]["content"]
        self.assertIn("left out to fit the context window", head)
        self.assertIn("Conversation began with: Help me plan the Q3 launch of Acme", head)
        self.assertIn("- what about pricing?", head)
        self.assertIn("- and the timeline?", head)
        self.assertGreaterEqual(fitted.omitted, 30)
        self.assertLessEqual(fitted.tokens, 400)

    def test_long_notes_are_cut_to_their_reserved_room_not_the_kept_turns(self):
        window = cw.Window(
            turns=conversation_turns(6, size=20),
            older_count=200,
            brief="B" * 5000,
            older_notes=["n" * 140] * 60,
        )
        fitted = cw.fit(window, "sys", 400)
        self.assertLessEqual(fitted.tokens, 400)
        self.assertEqual(fitted.turns[-1]["content"], "the current question")
        self.assertLess(len(fitted.turns[0]["content"]), 400 * cw.CHARS_PER_TOKEN)

    def test_the_original_turns_are_not_mutated(self):
        turns = conversation_turns(6)
        snapshot = [dict(t) for t in turns]
        cw.fit(cw.Window(turns=turns, older_count=5), "sys", 500)
        self.assertEqual(turns, snapshot)

    def test_a_current_message_that_cannot_fit_is_reported_not_cut(self):
        turns = [turn("user", "x" * 40_000)]
        fitted = cw.fit(cw.Window(turns=turns), "sys", 1000)
        self.assertFalse(fitted.fits)
        self.assertEqual(fitted.turns[-1]["content"], "x" * 40_000)

    def test_images_count_against_the_budget(self):
        turns = [turn("user", "look", images=[{"data": "x", "mime_type": "image/png"}]), turn("user", "now")]
        self.assertFalse(cw.fit(cw.Window(turns=turns), "sys", 500).turns[0]["content"] == "")
        self.assertEqual(len(cw.fit(cw.Window(turns=turns), "sys", 5000).turns), 2)
        self.assertEqual(len(cw.fit(cw.Window(turns=turns), "sys", 900).turns), 1)  # 1000-token image dropped

    def test_no_budget_means_no_trimming_of_what_was_loaded(self):
        turns = conversation_turns(5)
        self.assertEqual(cw.fit(cw.Window(turns=turns), "sys", None).turns, turns)

    def test_announcement_is_once_per_conversation_per_day(self):
        cache.clear()
        self.assertTrue(cw.should_announce(4242))
        self.assertFalse(cw.should_announce(4242))
        self.assertTrue(cw.should_announce(4243))
        with patch.object(cw.cache, "add", side_effect=RuntimeError("redis down")):
            self.assertTrue(cw.should_announce(1))


class ModelLimitTests(TestCase):
    def setUp(self):
        self.provider = Provider.objects.get(slug="openai")

    def model(self, model_id="some-model"):
        return ProviderModel(provider=self.provider, model_id=model_id)  # not saved: only read

    def test_every_model_has_a_finite_limit(self):
        for adapter in ("anthropic", "gemini", "openai_compatible"):
            provider = Provider.objects.filter(adapter_type=adapter).first()
            if provider is None:
                continue
            pm = ProviderModel(provider=provider, model_id="anything")
            self.assertTrue(1024 <= cw.model_context_tokens(pm) < 10**7, adapter)

    @override_settings(MODEL_CONTEXT_TOKENS_DEFAULT=12345, MODEL_CONTEXT_TOKENS={})
    def test_an_unknown_adapter_gets_the_configured_default(self):
        pm = self.model()
        self.assertEqual(cw.model_context_tokens(pm), 12345)

    @override_settings(
        MODEL_CONTEXT_TOKENS_BY_MODEL={"special": 77000}, MODEL_CONTEXT_TOKENS={"openai_compatible": 50000}
    )
    def test_a_model_override_beats_the_adapter_default(self):
        self.assertEqual(cw.model_context_tokens(self.model("my-SPECIAL-1")), 77000)
        self.assertEqual(cw.model_context_tokens(self.model("plain")), 50000)

    @override_settings(MODEL_CONTEXT_TOKENS_BY_MODEL={"tiny": 100})
    def test_the_budget_keeps_a_floor_and_reserves_room_for_the_reply(self):
        self.assertEqual(cw.model_budget(self.model("tiny")), 1024)
        self.assertEqual(
            cw.model_budget(self.model("normal")),
            cw.model_context_tokens(self.model("normal")) - cw.OUTPUT_RESERVE_TOKENS,
        )


class LoadWindowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(email="window@example.com", password="pw12345!")
        self.conversation = Conversation.objects.create(user=self.user)

    def add(self, role, text, **extra):
        return Message.objects.create(conversation=self.conversation, role=role, content=text, **extra)

    def fill(self, pairs):
        for i in range(pairs):
            self.add("user", f"question {i}")
            self.add("assistant", f"answer {i}")
        return self.add("assistant", "")  # the pending reply, excluded

    def test_only_the_newest_messages_are_built_and_older_ones_are_counted(self):
        pending = self.fill(60)  # 120 messages
        window = cw.load_window(self.conversation, pending.id)
        self.assertEqual(len(window.turns), cw.MAX_RECENT_MESSAGES)
        self.assertEqual(window.turns[-1]["content"], "answer 59")
        self.assertEqual(window.older_count, 120 - cw.MAX_RECENT_MESSAGES)
        self.assertEqual(window.brief, "question 0")
        self.assertEqual(window.older_notes[-1], "question 39")  # nearest to the kept window
        self.assertNotIn("question 60", window.older_notes)

    def test_the_cost_does_not_grow_with_conversation_length(self):
        pending = self.fill(30)
        with CaptureQueriesContext(connection) as short:
            cw.load_window(self.conversation, pending.id)
        pending = self.fill(150)
        with CaptureQueriesContext(connection) as long:
            cw.load_window(self.conversation, pending.id)
        self.assertEqual(len(short), len(long))

    def test_a_short_conversation_is_sent_whole_with_no_notes(self):
        pending = self.fill(3)
        window = cw.load_window(self.conversation, pending.id)
        self.assertEqual((len(window.turns), window.older_count, window.brief, window.older_notes), (6, 0, "", []))

    def test_only_the_newest_images_are_sent_as_images(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        for i in range(cw.MAX_IMAGES + 2):
            msg = self.add("user", f"pic {i}", attachment_original_name=f"p{i}.png")
            msg.attachment.save(f"p{i}.png", ContentFile(png), save=True)
        pending = self.add("assistant", "")
        window = cw.load_window(self.conversation, pending.id)
        with_images = [t["content"] for t in window.turns if t.get("images")]
        self.assertEqual(with_images, [f"pic {i}" for i in range(2, cw.MAX_IMAGES + 2)])
        self.assertIn("not re-sent", window.turns[0]["content"])
        self.assertIn("not re-sent", window.turns[1]["content"])


class AttachmentTextCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(email="att@example.com", password="pw12345!")
        self.conversation = Conversation.objects.create(user=self.user)

    def message(self, name="notes.txt", data=b"quarterly numbers", conversation=None):
        msg = Message.objects.create(
            conversation=conversation or self.conversation, role="user", content="see", attachment_original_name=name
        )
        msg.attachment.save(name, ContentFile(data), save=True)
        return msg

    def extractions(self, msg):
        with patch.object(cw, "extract_text", wraps=cw.extract_text) as spy:
            first = cw.attachment_text(msg, "txt")
            second = cw.attachment_text(msg, "txt")
        return first, second, spy.call_count

    def test_the_file_is_read_once_and_reused(self):
        first, second, calls = self.extractions(self.message())
        self.assertEqual((first, second, calls), ("quarterly numbers", "quarterly numbers", 1))

    def test_a_changed_file_is_read_again(self):
        msg = self.message()
        self.extractions(msg)
        with msg.attachment.storage.open(msg.attachment.name, "wb") as handle:
            handle.write(b"revised figures, longer than before")
        first, _second, calls = self.extractions(msg)
        self.assertEqual((first, calls), ("revised figures, longer than before", 1))

    def test_another_message_never_gets_this_messages_text(self):
        other_user = User.objects.create_user(email="att-other@example.com", password="pw12345!")
        mine = self.message(data=b"my private notes")
        theirs = self.message(data=b"their private notes", conversation=Conversation.objects.create(user=other_user))
        self.assertEqual(cw.attachment_text(mine, "txt"), "my private notes")
        self.assertEqual(cw.attachment_text(theirs, "txt"), "their private notes")
        self.assertNotEqual(cw._attachment_cache_key(mine), cw._attachment_cache_key(theirs))

    def test_a_file_that_cannot_be_read_is_remembered_briefly_and_never_raises(self):
        msg = self.message(name="empty.txt", data=b"   ")
        first, second, calls = self.extractions(msg)
        self.assertEqual((first, second, calls), (None, None, 1))

    def test_a_missing_file_degrades_to_unreadable(self):
        msg = self.message()
        msg.attachment.storage.delete(msg.attachment.name)
        self.assertIsNone(cw.attachment_text(msg, "txt"))

    def test_a_cache_outage_falls_back_to_extracting(self):
        msg = self.message()
        with patch.object(cw.cache, "get", side_effect=RuntimeError("down")), patch.object(
            cw.cache, "set", side_effect=RuntimeError("down")
        ):
            self.assertEqual(cw.attachment_text(msg, "txt"), "quarterly numbers")

    def test_text_is_bounded(self):
        msg = self.message(name="big.txt", data=b"w" * 50_000)
        self.assertLessEqual(len(cw.attachment_text(msg, "txt")), 8100)

    def test_document_text_never_reaches_a_log(self):
        msg = self.message(data=b"TOP-SECRET-FIGURES")
        with self.assertNoLogs(level="DEBUG"):
            cw.attachment_text(msg, "txt")


class StreamedReplyContextTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="ctx@example.com", password="pw12345!")
        self.model = ProviderModel.objects.create(
            provider=Provider.objects.get(slug="openai"),
            model_id="ctx-model",
            tier=ProviderModel.Tier.DEFAULT,
            input_price_per_mtok=1,
            output_price_per_mtok=2,
            is_enabled=True,
        )
        _grant_premium_plan(self.user, self.model)
        self.client.force_login(self.user)

    def set_plan_limit(self, tokens):
        Plan.objects.filter(name="Premium").update(max_context_tokens=tokens)

    def conversation_with(self, pairs, current="what now?", size=400):
        conversation = Conversation.objects.create(user=self.user)
        for i in range(pairs):
            Message.objects.create(conversation=conversation, role="user", content=f"question {i} " + "q" * size)
            Message.objects.create(conversation=conversation, role="assistant", content=f"answer {i} " + "a" * size)
        Message.objects.create(conversation=conversation, role="user", content=current)
        pending = Message.objects.create(conversation=conversation, role="assistant", content="")
        return conversation, pending

    def stream(self, conversation, pending, reply="Sure."):
        url = reverse(
            "chat:stream_message",
            kwargs={"conversation_id": conversation.id, "message_id": pending.id, "token": pending.stream_token},
        )
        sent = {}

        def fake_stream_chat(history, model_id, **kwargs):
            sent["history"] = history
            return iter([StreamChunk(text=reply), StreamChunk(done=True)])

        with patch("chat.views.classify_complexity", return_value=ProviderModel.Tier.DEFAULT), patch(
            "chat.views.build_system_prompt", return_value="sys"
        ), patch("chat.views.get_provider") as provider:
            provider.return_value.stream_chat.side_effect = fake_stream_chat
            body = b"".join(self.client.get(url).streaming_content).decode()
        pending.refresh_from_db()
        return sent.get("history"), body

    def test_a_long_conversation_is_fitted_to_the_plan_not_rejected(self):
        self.set_plan_limit(1500)
        conversation, pending = self.conversation_with(30)  # ~6000 tokens of history
        history, _body = self.stream(conversation, pending)
        self.assertIsNotNone(history, "the provider was never called")
        self.assertLess(len(history), 61)
        self.assertEqual(history[-1]["content"], "what now?")
        self.assertEqual(history[0]["role"], "user")
        self.assertIn("left out to fit the context window", history[0]["content"])
        self.assertLessEqual(cw.estimate_tokens("sys", history), 1500 + 50)
        pending.refresh_from_db()
        self.assertIn("older messages are condensed", pending.content)

    def test_the_notice_appears_once_not_under_every_reply(self):
        self.set_plan_limit(1500)
        conversation, pending = self.conversation_with(30)
        self.stream(conversation, pending)
        next_pending = Message.objects.create(conversation=conversation, role="assistant", content="")
        Message.objects.create(conversation=conversation, role="user", content="and then?")
        pending2 = Message.objects.create(conversation=conversation, role="assistant", content="")
        Message.objects.filter(pk=next_pending.pk).update(content="Sure.")
        history, _ = self.stream(conversation, pending2)
        self.assertIsNotNone(history)
        pending2.refresh_from_db()
        self.assertNotIn("condensed", pending2.content)

    def test_a_short_conversation_is_untouched_and_gets_no_notice(self):
        conversation, pending = self.conversation_with(2, size=10)
        history, _ = self.stream(conversation, pending)
        self.assertEqual(len(history), 5)
        self.assertNotIn("left out", history[0]["content"])
        self.assertEqual(pending.content, "Sure.")

    def test_a_current_message_over_the_plan_cap_gets_the_plan_message_and_no_provider_call(self):
        self.set_plan_limit(200)
        conversation, pending = self.conversation_with(0, current="z" * 5000)
        history, _ = self.stream(conversation, pending)
        self.assertIsNone(history)
        self.assertIn("per-request context limit", pending.content)

    @override_settings(MODEL_CONTEXT_TOKENS_BY_MODEL={"ctx-model": 9000})
    def test_a_message_over_the_models_window_gets_a_clear_error_without_provider_details(self):
        self.set_plan_limit(None)
        conversation, pending = self.conversation_with(0, current="z" * 20_000)
        history, _ = self.stream(conversation, pending)
        self.assertIsNone(history)
        self.assertIn("too large for the selected model", pending.content)
        for leak in ("ctx-model", "openai", "OpenAI", "9000", "Traceback"):
            self.assertNotIn(leak, pending.content)

    @override_settings(MODEL_CONTEXT_TOKENS_BY_MODEL={"ctx-model": 9000})
    def test_no_plan_cap_still_means_a_bounded_request(self):
        self.set_plan_limit(None)
        conversation, pending = self.conversation_with(40)  # ~16000 tokens of history
        history, _ = self.stream(conversation, pending)
        self.assertLessEqual(cw.estimate_tokens("sys", history), cw.model_budget(self.model) + 50)

    def test_the_condensing_note_is_not_cached_with_the_reply(self):
        self.set_plan_limit(1500)
        conversation, pending = self.conversation_with(30)
        with patch("chat.views.store_cached_response") as store:
            self.stream(conversation, pending, reply="the model's own words")
        self.assertEqual(store.call_args.kwargs["text"], "the model's own words")
