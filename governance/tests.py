from django.test import TestCase
from django.urls import reverse

from accounts.models import Department, User
from chat.models import Conversation, Message, ModelConfig
from chat.prompts import build_system_prompt
from governance.limits import UsageLimitExceeded, check_usage_limits
from governance.models import AuditLog, SystemPromptVersion, UsageLimit


class UsageLimitTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.user = User.objects.create_user(
            email="u@example.com", password="pw12345!", department=self.department,
        )
        self.conversation = Conversation.objects.create(user=self.user)
        self.model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI, model_name="m", is_enabled=True,
        )

    def _add_assistant_message(self, input_tokens, output_tokens, cost):
        Message.objects.create(
            conversation=self.conversation, role=Message.Role.ASSISTANT, content="hi",
            model_used=self.model, input_tokens=input_tokens, output_tokens=output_tokens,
            estimated_cost=cost,
        )

    def test_no_limit_configured_allows_sending(self):
        check_usage_limits(self.user, self.conversation)  # should not raise

    def test_daily_token_cap_blocks_when_reached(self):
        UsageLimit.objects.create(user=self.user, daily_token_cap=100)
        self._add_assistant_message(60, 60, 0)  # 120 tokens >= 100 cap
        with self.assertRaises(UsageLimitExceeded):
            check_usage_limits(self.user, self.conversation)

    def test_daily_token_cap_allows_when_under(self):
        UsageLimit.objects.create(user=self.user, daily_token_cap=1000)
        self._add_assistant_message(10, 10, 0)
        check_usage_limits(self.user, self.conversation)  # should not raise

    def test_budget_cap_blocks_when_reached(self):
        UsageLimit.objects.create(user=self.user, budget_cap_currency=5)
        self._add_assistant_message(1, 1, 5)
        with self.assertRaises(UsageLimitExceeded):
            check_usage_limits(self.user, self.conversation)

    def test_session_limit_blocks_when_reached(self):
        UsageLimit.objects.create(user=self.user, session_limit=1)
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="one")
        with self.assertRaises(UsageLimitExceeded):
            check_usage_limits(self.user, self.conversation)

    def test_department_limit_applies_when_no_personal_limit(self):
        UsageLimit.objects.create(department=self.department, daily_token_cap=50)
        self._add_assistant_message(30, 30, 0)
        with self.assertRaises(UsageLimitExceeded):
            check_usage_limits(self.user, self.conversation)

    def test_personal_limit_overrides_department_limit(self):
        UsageLimit.objects.create(department=self.department, daily_token_cap=1)
        UsageLimit.objects.create(user=self.user, daily_token_cap=10_000)
        self._add_assistant_message(30, 30, 0)
        check_usage_limits(self.user, self.conversation)  # personal limit wins, should not raise


class ChatPostMessageLimitIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="u@example.com", password="pw12345!")
        self.conversation = Conversation.objects.create(user=self.user)

    def test_post_message_blocked_when_session_limit_reached(self):
        UsageLimit.objects.create(user=self.user, session_limit=1)
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="one")

        response = self.client.post(
            reverse("chat:post_message", kwargs={"conversation_id": self.conversation.id}),
            {"content": "two"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.conversation.messages.filter(role=Message.Role.USER).count(), 1)

    def test_session_limit_is_scoped_per_conversation(self):
        """A user can hold multiple concurrent chat sessions (conversations);
        hitting the message cap in one must not block the others."""
        UsageLimit.objects.create(user=self.user, session_limit=1)
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="one")

        other_conversation = Conversation.objects.create(user=self.user)
        response = self.client.post(
            reverse("chat:post_message", kwargs={"conversation_id": other_conversation.id}),
            {"content": "hello from a different session"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(other_conversation.messages.filter(role=Message.Role.USER).count(), 1)


class SystemPromptInjectionTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Legal")
        self.user = User.objects.create_user(
            email="u@example.com", password="pw12345!", department=self.department,
        )

    def test_no_version_yields_blank_department_instructions(self):
        prompt = build_system_prompt(self.user)
        self.assertIn("Legal", prompt)

    def test_active_version_is_injected(self):
        SystemPromptVersion.objects.create_new_version(
            department=self.department, content="Always cite case law.",
            restricted_topics="tax advice",
        )
        prompt = build_system_prompt(self.user)
        self.assertIn("Always cite case law.", prompt)
        self.assertIn("tax advice", prompt)

    def test_only_one_active_version_per_department(self):
        v1 = SystemPromptVersion.objects.create_new_version(department=self.department, content="v1")
        v2 = SystemPromptVersion.objects.create_new_version(department=self.department, content="v2")
        v1.refresh_from_db()
        self.assertFalse(v1.is_active)
        self.assertTrue(v2.is_active)


class GovernanceRBACAndAuditTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True,
        )
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")

    def test_dashboard_forbidden_for_regular_user(self):
        self.client.login(email="u@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_dashboard_accessible_for_admin(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_toggle_user_active_writes_audit_log(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        self.assertTrue(self.user.is_active)
        response = self.client.post(
            reverse("governance:toggle_user_active", kwargs={"user_id": self.user.id})
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

        log = AuditLog.objects.get(action_type="user.suspend")
        self.assertEqual(log.actor, self.admin)
        self.assertEqual(log.target_id, str(self.user.id))

    def test_toggle_user_active_forbidden_for_non_admin(self):
        self.client.login(email="u@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:toggle_user_active", kwargs={"user_id": self.user.id})
        )
        self.assertEqual(response.status_code, 403)

    def test_change_user_role_writes_audit_log(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_role", kwargs={"user_id": self.user.id}),
            {"role": User.Role.MANAGER},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, User.Role.MANAGER)
        self.assertTrue(AuditLog.objects.filter(action_type="user.role_change").exists())

    def test_toggle_model_enabled_writes_audit_log(self):
        model_config = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI, model_name="m", is_enabled=False,
        )
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:toggle_model_enabled", kwargs={"model_id": model_config.id})
        )
        self.assertEqual(response.status_code, 302)
        model_config.refresh_from_db()
        self.assertTrue(model_config.is_enabled)
        self.assertTrue(AuditLog.objects.filter(action_type="model.enable").exists())

    def test_system_prompt_new_version_writes_audit_log(self):
        department = Department.objects.create(name="Ops")
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:system_prompt", kwargs={"department_id": department.id}),
            {"content": "Be concise.", "tone_preference": "casual", "restricted_topics": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SystemPromptVersion.objects.filter(department=department, is_active=True).exists())
        self.assertTrue(AuditLog.objects.filter(action_type="system_prompt.new_version").exists())
