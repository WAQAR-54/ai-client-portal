import tempfile
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from chat.models import Conversation, Message, ModelConfig, UserModelPermission
from chat.providers import ProviderError, StreamChunk, get_provider
from chat.router import NoModelAvailableError, classify_complexity, select_model_for_user


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
            provider=ModelConfig.Provider.OPENAI, model_name="cheap-model",
            tier=ModelConfig.Tier.ECONOMY, output_cost_per_1m=1, is_enabled=True,
        )
        self.default = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI, model_name="mid-model",
            tier=ModelConfig.Tier.DEFAULT, output_cost_per_1m=5, is_enabled=True,
        )

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
            provider=ModelConfig.Provider.OPENAI, model_name="test-model",
            tier=ModelConfig.Tier.DEFAULT, input_cost_per_1m=1, output_cost_per_1m=2, is_enabled=True,
        )
        self.client.login(email="u@example.com", password="pw12345!")

    def test_create_conversation_requires_login(self):
        self.client.logout()
        response = self.client.post(reverse("chat:create_conversation"))
        self.assertEqual(response.status_code, 302)

    def test_create_and_view_conversation(self):
        response = self.client.post(reverse("chat:create_conversation"))
        conversation = Conversation.objects.get(user=self.user)
        self.assertRedirects(
            response, reverse("chat:chat_conversation", kwargs={"conversation_id": conversation.id})
        )

    def test_cannot_access_other_users_conversation(self):
        conversation = Conversation.objects.create(user=self.other_user, title="private")
        response = self.client.get(
            reverse("chat:chat_conversation", kwargs={"conversation_id": conversation.id})
        )
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
            reverse("chat:post_message", kwargs={"conversation_id": conversation.id}), {"content": "  "},
        )
        self.assertEqual(response.status_code, 400)

    @patch("chat.views.classify_complexity", return_value=ModelConfig.Tier.DEFAULT)
    @patch("chat.views.get_provider")
    def test_stream_message_saves_assistant_reply(self, mock_get_provider, mock_classify):
        mock_provider = mock_get_provider.return_value
        mock_provider.stream_chat.return_value = iter([
            StreamChunk(text="Hel"),
            StreamChunk(text="lo!"),
            StreamChunk(done=True, input_tokens=10, output_tokens=5),
        ])

        conversation = Conversation.objects.create(user=self.user)
        Message.objects.create(conversation=conversation, role=Message.Role.USER, content="hi")
        pending = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")

        response = self.client.get(
            reverse("chat:stream_message", kwargs={
                "conversation_id": conversation.id, "message_id": pending.id,
            })
        )
        b"".join(response.streaming_content)

        pending.refresh_from_db()
        self.assertEqual(pending.content, "Hello!")
        self.assertEqual(pending.model_used, self.model)
        self.assertEqual(pending.input_tokens, 10)
        self.assertEqual(pending.output_tokens, 5)
        self.assertEqual(pending.estimated_cost, self.model.estimate_cost(10, 5))

    def test_stream_message_escapes_html_in_chunks(self):
        with patch("chat.views.classify_complexity", return_value=ModelConfig.Tier.DEFAULT), \
             patch("chat.views.get_provider") as mock_get_provider:
            mock_get_provider.return_value.stream_chat.return_value = iter([
                StreamChunk(text="<script>alert(1)</script>"),
                StreamChunk(done=True, input_tokens=1, output_tokens=1),
            ])
            conversation = Conversation.objects.create(user=self.user)
            pending = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")

            response = self.client.get(
                reverse("chat:stream_message", kwargs={
                    "conversation_id": conversation.id, "message_id": pending.id,
                })
            )
            body = b"".join(response.streaming_content).decode()
            self.assertNotIn("<script>", body)
            self.assertIn("&lt;script&gt;", body)

    @patch("chat.views.classify_complexity", return_value=ModelConfig.Tier.DEFAULT)
    @patch("chat.views.get_provider")
    def test_stream_message_falls_back_to_second_provider_on_failure(self, mock_get_provider, mock_classify):
        # self.model (openai) is cheaper and tried first; anthropic is the fallback.
        anthropic_model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.ANTHROPIC, model_name="fallback-model",
            tier=ModelConfig.Tier.DEFAULT, input_cost_per_1m=5, output_cost_per_1m=5, is_enabled=True,
        )

        failing_provider = MagicMock()
        failing_provider.stream_chat.side_effect = ProviderError("primary provider is down")
        working_provider = MagicMock()
        working_provider.stream_chat.return_value = iter([
            StreamChunk(text="fallback reply"),
            StreamChunk(done=True, input_tokens=3, output_tokens=4),
        ])

        def provider_for(name):
            return failing_provider if name == ModelConfig.Provider.OPENAI else working_provider

        mock_get_provider.side_effect = provider_for

        conversation = Conversation.objects.create(user=self.user)
        pending = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")

        response = self.client.get(
            reverse("chat:stream_message", kwargs={
                "conversation_id": conversation.id, "message_id": pending.id,
            })
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
        ModelConfig.objects.create(
            provider=ModelConfig.Provider.ANTHROPIC, model_name="also-down",
            tier=ModelConfig.Tier.DEFAULT, input_cost_per_1m=5, output_cost_per_1m=5, is_enabled=True,
        )
        broken_provider = MagicMock()
        broken_provider.stream_chat.side_effect = ProviderError("down")
        mock_get_provider.return_value = broken_provider

        conversation = Conversation.objects.create(user=self.user)
        pending = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")

        response = self.client.get(
            reverse("chat:stream_message", kwargs={
                "conversation_id": conversation.id, "message_id": pending.id,
            })
        )
        body = b"".join(response.streaming_content).decode()
        self.assertIn("event: error", body)
        pending.refresh_from_db()
        self.assertEqual(pending.content, "(request failed before completion)")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class FileUploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.other_user = User.objects.create_user(email="other@example.com", password="pw12345!")
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
            conversation=self.conversation, role=Message.Role.USER, content="",
            attachment=SimpleUploadedFile("notes.txt", b"secret"), attachment_original_name="notes.txt",
        )
        self.client.logout()
        self.client.login(email="other@example.com", password="pw12345!")
        response = self.client.get(
            reverse("chat:download_attachment", kwargs={
                "conversation_id": self.conversation.id, "message_id": message.id,
            })
        )
        self.assertEqual(response.status_code, 404)

    def test_download_attachment_works_for_owner(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        message = Message.objects.create(
            conversation=self.conversation, role=Message.Role.USER, content="",
            attachment=SimpleUploadedFile("notes.txt", b"secret"), attachment_original_name="notes.txt",
        )
        response = self.client.get(
            reverse("chat:download_attachment", kwargs={
                "conversation_id": self.conversation.id, "message_id": message.id,
            })
        )
        self.assertEqual(response.status_code, 200)


class ModelSelectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="u@example.com", password="pw12345!")
        self.allowed_model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI, model_name="allowed-model",
            tier=ModelConfig.Tier.DEFAULT, output_cost_per_1m=2, is_enabled=True,
        )
        self.denied_model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.ANTHROPIC, model_name="denied-model",
            tier=ModelConfig.Tier.DEFAULT, output_cost_per_1m=1, is_enabled=True,
        )
        UserModelPermission.objects.create(user=self.user, model_config=self.denied_model, is_allowed=False)
        self.conversation = Conversation.objects.create(user=self.user)

    @patch("chat.views.get_provider")
    def test_manually_selected_allowed_model_is_used(self, mock_get_provider):
        from chat.providers import StreamChunk

        mock_get_provider.return_value.stream_chat.return_value = iter([
            StreamChunk(text="hi"),
            StreamChunk(done=True, input_tokens=1, output_tokens=1),
        ])
        pending = Message.objects.create(conversation=self.conversation, role=Message.Role.ASSISTANT, content="")
        url = reverse("chat:stream_message", kwargs={
            "conversation_id": self.conversation.id, "message_id": pending.id,
        }) + f"?model_id={self.allowed_model.id}"
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
