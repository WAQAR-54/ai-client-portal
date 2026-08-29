import tempfile
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from chat.models import Conversation, Message, ModelConfig, UserModelPermission
from chat.providers import ProviderError, StreamChunk, get_provider
from chat.router import NoModelAvailableError, classify_complexity, select_model_for_user


def _grant_premium_plan(user, *models):
    """Test helper: several unit tests below need a user to be able to use
    arbitrary fixture models, independent of the Plan/Tier system under test
    elsewhere. Premium's allowed_models is seeded empty for any ModelConfig
    created after the seed migration ran (true of every test fixture), so
    tests that need model access must explicitly grant it - mirroring the
    real admin action of enabling a model on a plan."""
    from governance.models import Plan
    from governance.plans import assign_plan

    premium = Plan.objects.get(name="Premium")
    assign_plan(user, premium)
    if models:
        premium.allowed_models.add(*models)
    return premium


class ProviderRegistryTests(TestCase):
    def test_unknown_provider_raises(self):
        with self.assertRaises(ProviderError):
            get_provider("does-not-exist")

    def test_known_providers_resolve(self):
        self.assertIsNotNone(get_provider("openai"))
        self.assertIsNotNone(get_provider("anthropic"))


class RouterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.economy = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="cheap-model",
            tier=ModelConfig.Tier.ECONOMY,
            output_cost_per_1m=1,
            is_enabled=True,
        )
        self.default = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="mid-model",
            tier=ModelConfig.Tier.DEFAULT,
            output_cost_per_1m=5,
            is_enabled=True,
        )
        _grant_premium_plan(self.user, self.economy, self.default)

    def test_classify_falls_back_to_default_when_no_economy_model(self):
        ModelConfig.objects.all().delete()
        self.assertEqual(classify_complexity("hello"), ModelConfig.Tier.DEFAULT)

    @patch("chat.router.get_provider")
    def test_classify_uses_cheapest_economy_model(self, mock_get_provider):
        mock_provider = mock_get_provider.return_value
        mock_provider.complete.return_value = "premium"
        result = classify_complexity("write a complex legal analysis")
        self.assertEqual(result, ModelConfig.Tier.PREMIUM)
        mock_get_provider.assert_called_once_with("openai")
        called_model = mock_provider.complete.call_args[0][1]
        self.assertEqual(called_model, "cheap-model")

    def test_select_model_respects_denied_permission(self):
        UserModelPermission.objects.create(user=self.user, model_config=self.economy, is_allowed=False)
        selected = select_model_for_user(self.user, ModelConfig.Tier.ECONOMY)
        self.assertEqual(selected, self.default)

    def test_select_model_raises_when_nothing_available(self):
        ModelConfig.objects.all().delete()
        with self.assertRaises(NoModelAvailableError):
            select_model_for_user(self.user, ModelConfig.Tier.DEFAULT)

    def test_select_model_falls_back_across_tiers(self):
        self.default.is_enabled = False
        self.default.save()
        selected = select_model_for_user(self.user, ModelConfig.Tier.DEFAULT)
        self.assertEqual(selected, self.economy)


class ChatViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.other_user = User.objects.create_user(email="other@example.com", password="pw12345!")
        self.model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="test-model",
            tier=ModelConfig.Tier.DEFAULT,
            input_cost_per_1m=1,
            output_cost_per_1m=2,
            is_enabled=True,
        )
        self.premium = _grant_premium_plan(self.user, self.model)
        self.client.login(email="u@example.com", password="pw12345!")

    def test_create_conversation_requires_login(self):
        self.client.logout()
        response = self.client.post(reverse("chat:create_conversation"))
        self.assertEqual(response.status_code, 302)

    def test_create_and_view_conversation(self):
        response = self.client.post(reverse("chat:create_conversation"))
        conversation = Conversation.objects.get(user=self.user)
        self.assertRedirects(response, reverse("chat:chat_conversation", kwargs={"conversation_id": conversation.id}))

    def test_cannot_access_other_users_conversation(self):
        conversation = Conversation.objects.create(user=self.other_user, title="private")
        response = self.client.get(reverse("chat:chat_conversation", kwargs={"conversation_id": conversation.id}))
        self.assertEqual(response.status_code, 404)

    def test_post_message_creates_user_and_pending_assistant_message(self):
        conversation = Conversation.objects.create(user=self.user)
        response = self.client.post(
            reverse("chat:post_message", kwargs={"conversation_id": conversation.id}),
            {"content": "hello there"},
        )
        self.assertEqual(response.status_code, 200)
        messages = list(conversation.messages.all())
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, Message.Role.USER)
        self.assertEqual(messages[0].content, "hello there")
        self.assertEqual(messages[1].role, Message.Role.ASSISTANT)
        self.assertEqual(messages[1].content, "")

    def test_post_message_rejects_empty_content(self):
        conversation = Conversation.objects.create(user=self.user)
        response = self.client.post(
            reverse("chat:post_message", kwargs={"conversation_id": conversation.id}),
            {"content": "  "},
        )
        self.assertEqual(response.status_code, 400)

    @patch("chat.views.classify_complexity", return_value=ModelConfig.Tier.DEFAULT)
    @patch("chat.views.get_provider")
    def test_stream_message_saves_assistant_reply(self, mock_get_provider, mock_classify):
        mock_provider = mock_get_provider.return_value
        mock_provider.stream_chat.return_value = iter(
            [
                StreamChunk(text="Hel"),
                StreamChunk(text="lo!"),
                StreamChunk(done=True, input_tokens=10, output_tokens=5),
            ]
        )

        conversation = Conversation.objects.create(user=self.user)
        Message.objects.create(conversation=conversation, role=Message.Role.USER, content="hi")
        pending = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")

        response = self.client.get(
            reverse(
                "chat:stream_message",
                kwargs={
                    "conversation_id": conversation.id,
                    "message_id": pending.id,
                },
            )
        )
        b"".join(response.streaming_content)

        pending.refresh_from_db()
        self.assertEqual(pending.content, "Hello!")
        self.assertEqual(pending.model_used, self.model)
        self.assertEqual(pending.input_tokens, 10)
        self.assertEqual(pending.output_tokens, 5)
        self.assertEqual(pending.estimated_cost, self.model.estimate_cost(10, 5))

    def test_stream_message_escapes_html_in_chunks(self):
        with patch("chat.views.classify_complexity", return_value=ModelConfig.Tier.DEFAULT), patch(
            "chat.views.get_provider"
        ) as mock_get_provider:
            mock_get_provider.return_value.stream_chat.return_value = iter(
                [
                    StreamChunk(text="<script>alert(1)</script>"),
                    StreamChunk(done=True, input_tokens=1, output_tokens=1),
                ]
            )
            conversation = Conversation.objects.create(user=self.user)
            pending = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")

            response = self.client.get(
                reverse(
                    "chat:stream_message",
                    kwargs={
                        "conversation_id": conversation.id,
                        "message_id": pending.id,
                    },
                )
            )
            body = b"".join(response.streaming_content).decode()
            self.assertNotIn("<script>", body)
            self.assertIn("&lt;script&gt;", body)

    @patch("chat.views.classify_complexity", return_value=ModelConfig.Tier.DEFAULT)
    @patch("chat.views.get_provider")
    def test_stream_message_falls_back_to_second_provider_on_failure(self, mock_get_provider, mock_classify):
        # self.model (openai) is cheaper and tried first; anthropic is the fallback.
        anthropic_model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.ANTHROPIC,
            model_name="fallback-model",
            tier=ModelConfig.Tier.DEFAULT,
            input_cost_per_1m=5,
            output_cost_per_1m=5,
            is_enabled=True,
        )
        self.premium.allowed_models.add(anthropic_model)

        failing_provider = MagicMock()
        failing_provider.stream_chat.side_effect = ProviderError("primary provider is down")
        working_provider = MagicMock()
        working_provider.stream_chat.return_value = iter(
            [
                StreamChunk(text="fallback reply"),
                StreamChunk(done=True, input_tokens=3, output_tokens=4),
            ]
        )

        def provider_for(name):
            return failing_provider if name == ModelConfig.Provider.OPENAI else working_provider

        mock_get_provider.side_effect = provider_for

        conversation = Conversation.objects.create(user=self.user)
        pending = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")

        response = self.client.get(
            reverse(
                "chat:stream_message",
                kwargs={
                    "conversation_id": conversation.id,
                    "message_id": pending.id,
                },
            )
        )
        body = b"".join(response.streaming_content).decode()

        self.assertIn("fallback reply", body)
        self.assertNotIn("event: error", body)
        pending.refresh_from_db()
        self.assertEqual(pending.content, "fallback reply")
        self.assertEqual(pending.model_used, anthropic_model)

    @patch("chat.views.classify_complexity", return_value=ModelConfig.Tier.DEFAULT)
    @patch("chat.views.get_provider")
    def test_stream_message_fails_gracefully_when_all_providers_down(self, mock_get_provider, mock_classify):
        also_down = ModelConfig.objects.create(
            provider=ModelConfig.Provider.ANTHROPIC,
            model_name="also-down",
            tier=ModelConfig.Tier.DEFAULT,
            input_cost_per_1m=5,
            output_cost_per_1m=5,
            is_enabled=True,
        )
        self.premium.allowed_models.add(also_down)
        broken_provider = MagicMock()
        broken_provider.stream_chat.side_effect = ProviderError("down")
        mock_get_provider.return_value = broken_provider

        conversation = Conversation.objects.create(user=self.user)
        pending = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")

        response = self.client.get(
            reverse(
                "chat:stream_message",
                kwargs={
                    "conversation_id": conversation.id,
                    "message_id": pending.id,
                },
            )
        )
        body = b"".join(response.streaming_content).decode()
        # a literal "event: error" is never emitted - it collides with
        # EventSource's own reserved connection-error event and htmx never
        # applies the swap for it (confirmed by hand). Every outcome routes
        # through "done" instead, saving the user-facing message to the DB
        # first so the render_message swap shows it.
        self.assertIn("event: done", body)
        self.assertNotIn("event: error", body)
        pending.refresh_from_db()
        self.assertEqual(pending.content, "The assistant hit a problem generating a response. Please try again.")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class FileUploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.other_user = User.objects.create_user(email="other@example.com", password="pw12345!")
        _grant_premium_plan(self.user)
        self.client.login(email="u@example.com", password="pw12345!")
        self.conversation = Conversation.objects.create(user=self.user)

    def test_upload_within_default_limits_succeeds(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("notes.txt", b"hello world", content_type="text/plain")
        response = self.client.post(
            reverse("chat:post_message", kwargs={"conversation_id": self.conversation.id}),
            {"content": "", "attachment": upload},
        )
        self.assertEqual(response.status_code, 200)
        user_message = self.conversation.messages.get(role=Message.Role.USER)
        self.assertEqual(user_message.attachment_original_name, "notes.txt")
        self.assertEqual(user_message.attachment_size, 11)

    def test_disallowed_extension_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("virus.exe", b"x" * 10, content_type="application/octet-stream")
        response = self.client.post(
            reverse("chat:post_message", kwargs={"conversation_id": self.conversation.id}),
            {"content": "", "attachment": upload},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.conversation.messages.filter(role=Message.Role.USER).exists())

    def test_oversized_file_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        upload = SimpleUploadedFile("big.txt", b"x" * 2_000_000, content_type="text/plain")
        with override_settings(DEFAULT_MAX_UPLOAD_SIZE_MB=1):
            response = self.client.post(
                reverse("chat:post_message", kwargs={"conversation_id": self.conversation.id}),
                {"content": "", "attachment": upload},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.conversation.messages.filter(role=Message.Role.USER).exists())

    def test_download_attachment_requires_ownership(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        message = Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.USER,
            content="",
            attachment=SimpleUploadedFile("notes.txt", b"secret"),
            attachment_original_name="notes.txt",
        )
        self.client.logout()
        self.client.login(email="other@example.com", password="pw12345!")
        response = self.client.get(
            reverse(
                "chat:download_attachment",
                kwargs={
                    "conversation_id": self.conversation.id,
                    "message_id": message.id,
                },
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_download_attachment_works_for_owner(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        message = Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.USER,
            content="",
            attachment=SimpleUploadedFile("notes.txt", b"secret"),
            attachment_original_name="notes.txt",
        )
        response = self.client.get(
            reverse(
                "chat:download_attachment",
                kwargs={
                    "conversation_id": self.conversation.id,
                    "message_id": message.id,
                },
            )
        )
        self.assertEqual(response.status_code, 200)


class ModelSelectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="u@example.com", password="pw12345!")
        self.allowed_model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="allowed-model",
            tier=ModelConfig.Tier.DEFAULT,
            output_cost_per_1m=2,
            is_enabled=True,
        )
        self.denied_model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.ANTHROPIC,
            model_name="denied-model",
            tier=ModelConfig.Tier.DEFAULT,
            output_cost_per_1m=1,
            is_enabled=True,
        )
        UserModelPermission.objects.create(user=self.user, model_config=self.denied_model, is_allowed=False)
        _grant_premium_plan(self.user, self.allowed_model, self.denied_model)
        self.conversation = Conversation.objects.create(user=self.user)

    @patch("chat.views.get_provider")
    def test_manually_selected_allowed_model_is_used(self, mock_get_provider):
        from chat.providers import StreamChunk

        mock_get_provider.return_value.stream_chat.return_value = iter(
            [
                StreamChunk(text="hi"),
                StreamChunk(done=True, input_tokens=1, output_tokens=1),
            ]
        )
        pending = Message.objects.create(conversation=self.conversation, role=Message.Role.ASSISTANT, content="")
        url = (
            reverse(
                "chat:stream_message",
                kwargs={
                    "conversation_id": self.conversation.id,
                    "message_id": pending.id,
                },
            )
            + f"?model_id={self.allowed_model.id}"
        )
        response = self.client.get(url)
        b"".join(response.streaming_content)
        pending.refresh_from_db()
        self.assertEqual(pending.model_used, self.allowed_model)

    def test_denied_model_id_is_ignored_in_post_message(self):
        response = self.client.post(
            reverse("chat:post_message", kwargs={"conversation_id": self.conversation.id}),
            {"content": "hi", "model_id": str(self.denied_model.id)},
        )
        self.assertEqual(response.status_code, 200)
        # the pending fragment's SSE URL should not carry the denied model's id
        self.assertNotIn(f"model_id={self.denied_model.id}", response.content.decode())


class MarkdownRenderingTests(TestCase):
    def test_bold_and_code_render(self):
        from chat.markdown_utils import render_markdown

        html = render_markdown("**bold** and `code`")
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<code>code</code>", html)

    def test_script_tags_are_stripped(self):
        from chat.markdown_utils import render_markdown

        html = render_markdown("<script>alert(1)</script>hello")
        self.assertNotIn("<script>", html)
        self.assertIn("hello", html)

    def test_javascript_protocol_links_are_stripped(self):
        from chat.markdown_utils import render_markdown

        html = render_markdown("[click me](javascript:alert(1))")
        self.assertNotIn("javascript:", html)


class UsageWidgetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.other_user = User.objects.create_user(email="other@example.com", password="pw12345!")
        self.client.login(email="u@example.com", password="pw12345!")

    def test_widget_shows_no_limits_message_when_none_configured(self):
        from governance.models import UserPlanAssignment

        # This test needs a user with genuinely no limit source at all -
        # not even a Plan's baked-in caps - to exercise the "no limits set"
        # empty state. Every new user is auto-assigned the Demo plan (which
        # has its own real caps), so explicitly remove that assignment
        # rather than relying on it never having been created.
        UserPlanAssignment.objects.filter(user=self.user).delete()
        response = self.client.get(reverse("chat:usage_widget"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("No usage limits set", body)
        self.assertNotIn("usage-bar", body)

    def test_widget_shows_metrics_when_limit_configured(self):
        from governance.models import UsageLimit

        UsageLimit.objects.create(user=self.user, daily_token_cap=1000)
        Message.objects.create(
            conversation=Conversation.objects.create(user=self.user),
            role=Message.Role.ASSISTANT,
            input_tokens=400,
            output_tokens=200,
        )
        response = self.client.get(reverse("chat:usage_widget"))
        body = response.content.decode()
        self.assertIn("Tokens today", body)
        self.assertIn("600", body)

    def test_widget_never_shows_another_users_usage(self):
        from governance.models import UsageLimit

        UsageLimit.objects.create(user=self.other_user, daily_token_cap=1000)
        Message.objects.create(
            conversation=Conversation.objects.create(user=self.other_user),
            role=Message.Role.ASSISTANT,
            input_tokens=999,
            output_tokens=1,
        )
        # logged in as self.user, who has no limit configured
        response = self.client.get(reverse("chat:usage_widget"))
        self.assertNotIn("1000", response.content.decode())


class RenderMessageViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.other_user = User.objects.create_user(email="other@example.com", password="pw12345!")
        self.conversation = Conversation.objects.create(user=self.user)
        self.client.login(email="u@example.com", password="pw12345!")

    def test_renders_markdown_for_owner(self):
        message = Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.ASSISTANT,
            content="**bold reply**",
        )
        response = self.client.get(
            reverse(
                "chat:render_message",
                kwargs={
                    "conversation_id": self.conversation.id,
                    "message_id": message.id,
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("<strong>bold reply</strong>", response.content.decode())

    def test_other_user_cannot_render_message(self):
        message = Message.objects.create(conversation=self.conversation, role=Message.Role.ASSISTANT, content="hi")
        self.client.logout()
        self.client.login(email="other@example.com", password="pw12345!")
        response = self.client.get(
            reverse(
                "chat:render_message",
                kwargs={
                    "conversation_id": self.conversation.id,
                    "message_id": message.id,
                },
            )
        )
        self.assertEqual(response.status_code, 404)


class ConversationGroupingTests(TestCase):
    def test_group_conversations_buckets_by_recency(self):
        from chat.utils import group_conversations

        now = timezone.now()
        user = User.objects.create_user(email="grp@example.com", password="pw12345!")
        today = Conversation.objects.create(user=user, title="today")
        old = Conversation.objects.create(user=user, title="old one")
        Conversation.objects.filter(pk=old.pk).update(updated_at=now - timezone.timedelta(days=40))

        ordered = Conversation.objects.filter(user=user).order_by("-updated_at")
        buckets = group_conversations(ordered)
        labels = [label for label, _ in buckets]

        self.assertIn("Today", labels)
        self.assertNotIn("Yesterday", labels)
        self.assertEqual(dict(buckets)["Today"], [today])
        self.assertNotIn(old, dict(buckets).get("Today", []))


class ConversationPinDeleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="pin@example.com", password="pw12345!")
        self.other_user = User.objects.create_user(email="pinother@example.com", password="pw12345!")
        self.conversation = Conversation.objects.create(user=self.user, title="mine")
        self.client.login(email="pin@example.com", password="pw12345!")

    def test_toggle_pin_sets_and_clears_pinned_at(self):
        response = self.client.post(reverse("chat:toggle_pin", kwargs={"conversation_id": self.conversation.id}))
        self.assertEqual(response.status_code, 200)
        self.conversation.refresh_from_db()
        self.assertTrue(self.conversation.is_pinned)
        self.assertIsNotNone(self.conversation.pinned_at)

        self.client.post(reverse("chat:toggle_pin", kwargs={"conversation_id": self.conversation.id}))
        self.conversation.refresh_from_db()
        self.assertFalse(self.conversation.is_pinned)
        self.assertIsNone(self.conversation.pinned_at)

    def test_cannot_pin_other_users_conversation(self):
        other_conversation = Conversation.objects.create(user=self.other_user, title="not mine")
        response = self.client.post(reverse("chat:toggle_pin", kwargs={"conversation_id": other_conversation.id}))
        self.assertEqual(response.status_code, 404)

    def test_delete_conversation_is_soft_and_hides_from_owner(self):
        response = self.client.post(
            reverse("chat:delete_conversation", kwargs={"conversation_id": self.conversation.id})
        )
        self.assertEqual(response.status_code, 200)

        self.assertFalse(Conversation.objects.filter(id=self.conversation.id).exists())
        self.assertTrue(Conversation.all_objects.filter(id=self.conversation.id, is_deleted=True).exists())

        # Soft-deleted conversations 404 for their owner like they don't exist.
        detail_response = self.client.get(
            reverse("chat:chat_conversation", kwargs={"conversation_id": self.conversation.id})
        )
        self.assertEqual(detail_response.status_code, 404)

    def test_delete_conversation_writes_audit_log_visible_only_to_admin_view(self):
        from governance.models import AuditLog

        self.client.post(reverse("chat:delete_conversation", kwargs={"conversation_id": self.conversation.id}))
        log = AuditLog.objects.get(action_type="conversation.delete")
        self.assertEqual(log.actor, self.user)
        self.assertEqual(log.old_value, "mine")
        self.assertEqual(log.target_id, str(self.conversation.id))

        # The deleting user's own chat_home response never surfaces AuditLog data.
        home_response = self.client.get(reverse("chat:chat_home"))
        self.assertNotIn(b"conversation.delete", home_response.content)

    def test_cannot_delete_other_users_conversation(self):
        other_conversation = Conversation.objects.create(user=self.other_user, title="not mine")
        response = self.client.post(
            reverse("chat:delete_conversation", kwargs={"conversation_id": other_conversation.id})
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Conversation.objects.filter(id=other_conversation.id).exists())

    def test_deleting_currently_open_conversation_redirects(self):
        response = self.client.post(
            reverse("chat:delete_conversation", kwargs={"conversation_id": self.conversation.id}),
            HTTP_HX_CURRENT_URL=f"http://testserver/chat/conversations/{self.conversation.id}/",
        )
        self.assertEqual(response["HX-Redirect"], reverse("chat:chat_home"))

    def test_sidebar_lists_all_of_users_conversations(self):
        Conversation.objects.create(user=self.user, title="second")
        Conversation.objects.create(user=self.user, title="third")
        response = self.client.get(reverse("chat:chat_home"))
        self.assertIn(b"mine", response.content)
        self.assertIn(b"second", response.content)
        self.assertIn(b"third", response.content)
