from django.test import TestCase
from django.urls import reverse

from accounts.models import Department, User
from chat.models import Conversation, Message, ModelConfig, UserModelPermission
from chat.prompts import build_system_prompt
from governance.limits import UploadRejected, UsageLimitExceeded, check_usage_limits, validate_upload
from governance.models import AuditLog, SystemPromptVersion, UsageLimit


class UsageLimitTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.user = User.objects.create_user(
            email="u@example.com",
            password="pw12345!",
            department=self.department,
        )
        self.conversation = Conversation.objects.create(user=self.user)
        self.model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="m",
            is_enabled=True,
        )

    def _add_assistant_message(self, input_tokens, output_tokens, cost):
        Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.ASSISTANT,
            content="hi",
            model_used=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
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
            email="u@example.com",
            password="pw12345!",
            department=self.department,
        )

    def test_no_version_yields_blank_department_instructions(self):
        prompt = build_system_prompt(self.user)
        self.assertIn("Legal", prompt)

    def test_active_version_is_injected(self):
        SystemPromptVersion.objects.create_new_version(
            department=self.department,
            content="Always cite case law.",
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
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
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
        response = self.client.post(reverse("governance:toggle_user_active", kwargs={"user_id": self.user.id}))
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

        log = AuditLog.objects.get(action_type="user.suspend")
        self.assertEqual(log.actor, self.admin)
        self.assertEqual(log.target_id, str(self.user.id))

    def test_toggle_user_active_forbidden_for_non_admin(self):
        self.client.login(email="u@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_user_active", kwargs={"user_id": self.user.id}))
        self.assertEqual(response.status_code, 403)

    def test_dashboard_charts_zero_filled_across_14_days(self):
        conversation = Conversation.objects.create(user=self.user, title="c")
        model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="m",
            is_enabled=True,
            input_cost_per_1m=1,
            output_cost_per_1m=1,
        )
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="hi",
            model_used=model,
            input_tokens=100,
            output_tokens=50,
            estimated_cost="0.15",
        )

        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:dashboard"))

        self.assertEqual(len(response.context["chart_daily_labels"]), 14)
        self.assertTrue(response.context["has_cost_data"])
        self.assertTrue(response.context["has_token_data"])
        self.assertTrue(response.context["has_model_data"])

    def test_dashboard_charts_show_empty_state_flags_with_no_usage(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:dashboard"))

        self.assertFalse(response.context["has_cost_data"])
        self.assertFalse(response.context["has_token_data"])
        self.assertFalse(response.context["has_model_data"])
        self.assertIn(b"No usage yet", response.content)

    def test_dashboard_chart_data_cannot_break_out_of_script_tag(self):
        """A model_name containing "</script>" must never let the dashboard's
        chart data terminate the real <script> block early - regression test
        for the chart-data-XSS fix (json_script instead of |safe)."""
        conversation = Conversation.objects.create(user=self.user, title="c")
        model = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="</script><script>alert(1)</script>",
            is_enabled=True,
            input_cost_per_1m=1,
            output_cost_per_1m=1,
        )
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="hi",
            model_used=model,
            input_tokens=10,
            output_tokens=5,
            estimated_cost="0.01",
        )

        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:dashboard"))
        body = response.content.decode()

        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("\\u003C/script\\u003E", body)

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

    def test_change_user_department_writes_audit_log(self):
        department = Department.objects.create(name="Support")
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_department", kwargs={"user_id": self.user.id}),
            {"department_id": department.id},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.department_id, department.id)
        self.assertTrue(AuditLog.objects.filter(action_type="user.department_change").exists())

    def test_change_user_department_to_none(self):
        department = Department.objects.create(name="Support")
        self.user.department = department
        self.user.save(update_fields=["department"])
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_department", kwargs={"user_id": self.user.id}),
            {"department_id": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.department_id)

    def test_toggle_model_enabled_writes_audit_log(self):
        model_config = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="m",
            is_enabled=False,
        )
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_model_enabled", kwargs={"model_id": model_config.id}))
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

    def test_add_model_creates_disabled_unpriced_entry(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:add_model"),
            {
                "provider": ModelConfig.Provider.OPENAI,
                "model_name": "gpt-new",
                "tier": ModelConfig.Tier.DEFAULT,
            },
        )
        self.assertEqual(response.status_code, 302)
        model_config = ModelConfig.objects.get(model_name="gpt-new")
        self.assertFalse(model_config.is_enabled)
        self.assertIsNone(model_config.input_cost_per_1m)

    def test_update_model_pricing(self):
        model_config = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m")
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:update_model_pricing", kwargs={"model_id": model_config.id}),
            {"input_cost_per_1m": "1.5", "output_cost_per_1m": "3.0"},
        )
        self.assertEqual(response.status_code, 302)
        model_config.refresh_from_db()
        self.assertEqual(str(model_config.input_cost_per_1m), "1.5000")
        self.assertEqual(str(model_config.output_cost_per_1m), "3.0000")

    def test_model_permissions_toggle_denies_and_reallows(self):
        model_config = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m", is_enabled=True)
        self.client.login(email="admin@example.com", password="pw12345!")

        response = self.client.post(
            reverse("governance:model_permissions", kwargs={"model_id": model_config.id}),
            {"user_id": self.user.id},
        )
        self.assertEqual(response.status_code, 302)
        permission = UserModelPermission.objects.get(user=self.user, model_config=model_config)
        self.assertFalse(permission.is_allowed)

        self.client.post(
            reverse("governance:model_permissions", kwargs={"model_id": model_config.id}),
            {"user_id": self.user.id},
        )
        permission.refresh_from_db()
        self.assertTrue(permission.is_allowed)


class LimitManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
        )
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="admin@example.com", password="pw12345!")

    def test_create_user_limit(self):
        response = self.client.post(
            reverse("governance:limit_new"),
            {
                "target_type": "user",
                "user_id": self.user.id,
                "daily_token_cap": "1000",
                "max_upload_size_mb": "5",
                "allowed_file_extensions": "pdf,txt",
            },
        )
        self.assertEqual(response.status_code, 302)
        limit = UsageLimit.objects.get(user=self.user)
        self.assertEqual(limit.daily_token_cap, 1000)
        self.assertEqual(limit.max_upload_size_mb, 5)

    def test_create_limit_without_target_rejected(self):
        response = self.client.post(reverse("governance:limit_new"), {"target_type": "user"})
        self.assertEqual(response.status_code, 400)

    def test_delete_limit(self):
        limit = UsageLimit.objects.create(user=self.user, daily_token_cap=500)
        response = self.client.post(reverse("governance:limit_delete", kwargs={"limit_id": limit.id}))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(UsageLimit.objects.filter(id=limit.id).exists())


class UploadLimitOverrideTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")

    def test_system_default_extension_check(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("script.exe", b"x", content_type="application/octet-stream")
        with self.assertRaises(UploadRejected):
            validate_upload(self.user, bad)

    def test_personal_limit_overrides_default_extensions(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        UsageLimit.objects.create(user=self.user, allowed_file_extensions="exe")
        upload = SimpleUploadedFile("tool.exe", b"x", content_type="application/octet-stream")
        validate_upload(self.user, upload)  # should not raise


class DepartmentManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
        )
        self.client.login(email="admin@example.com", password="pw12345!")

    def test_add_department(self):
        response = self.client.post(
            reverse("governance:add_department"),
            {
                "name": "Engineering",
                "monthly_budget_cap": "500",
            },
        )
        self.assertEqual(response.status_code, 302)
        department = Department.objects.get(name="Engineering")
        self.assertEqual(str(department.monthly_budget_cap), "500.00")
        self.assertTrue(AuditLog.objects.filter(action_type="department.add").exists())

    def test_add_department_requires_name(self):
        response = self.client.post(reverse("governance:add_department"), {"name": ""})
        self.assertEqual(response.status_code, 400)

    def test_update_department(self):
        department = Department.objects.create(name="Old Name")
        response = self.client.post(
            reverse("governance:update_department", kwargs={"department_id": department.id}),
            {"name": "New Name", "monthly_budget_cap": "250.50"},
        )
        self.assertEqual(response.status_code, 302)
        department.refresh_from_db()
        self.assertEqual(department.name, "New Name")
        self.assertEqual(str(department.monthly_budget_cap), "250.50")

    def test_delete_department(self):
        department = Department.objects.create(name="Temp")
        response = self.client.post(reverse("governance:delete_department", kwargs={"department_id": department.id}))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Department.objects.filter(id=department.id).exists())

    def test_non_admin_cannot_manage_departments(self):
        User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="u@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:add_department"), {"name": "Nope"})
        self.assertEqual(response.status_code, 403)


class AdminListFilteringTests(TestCase):
    """Search/filter query-param handling for the admin list screens, and
    the htmx-partial vs full-page template switch it relies on."""

    def setUp(self):
        self.admin = User.objects.create_user(
            email="filteradmin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
        )
        self.client.login(email="filteradmin@example.com", password="pw12345!")
        self.eng = Department.objects.create(name="Engineering")
        self.sales = Department.objects.create(name="Sales")
        self.alice = User.objects.create_user(email="alice@example.com", password="pw12345!", department=self.eng)
        self.bob = User.objects.create_user(
            email="bob@example.com",
            password="pw12345!",
            role=User.Role.MANAGER,
            department=self.sales,
        )

    def test_users_search_filters_by_email(self):
        response = self.client.get(reverse("governance:users"), {"search": "alice"})
        self.assertEqual(list(response.context["users"]), [self.alice])

    def test_users_role_filter(self):
        response = self.client.get(reverse("governance:users"), {"role": "manager"})
        self.assertEqual(list(response.context["users"]), [self.bob])

    def test_users_status_filter(self):
        self.bob.is_active = False
        self.bob.save()
        response = self.client.get(reverse("governance:users"), {"status": "suspended"})
        self.assertEqual(list(response.context["users"]), [self.bob])

    def test_users_htmx_request_gets_partial_template_only(self):
        response = self.client.get(reverse("governance:users"), {"search": "alice"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        # The partial has no <html>/sidebar chrome - base.html content wouldn't appear.
        self.assertNotIn(b"<html", response.content)
        self.assertIn(b"alice@example.com", response.content)

    def test_models_status_filter(self):
        enabled = ModelConfig.objects.create(provider="openai", model_name="gpt-4o-mini", is_enabled=True)
        disabled = ModelConfig.objects.create(provider="openai", model_name="gpt-3.5-turbo", is_enabled=False)
        response = self.client.get(reverse("governance:models"), {"status": "enabled"})
        self.assertEqual(list(response.context["models"]), [enabled])
        response = self.client.get(reverse("governance:models"), {"status": "disabled"})
        self.assertEqual(list(response.context["models"]), [disabled])

    def test_departments_search(self):
        response = self.client.get(reverse("governance:departments"), {"search": "eng"})
        self.assertEqual(list(response.context["departments"]), [self.eng])

    def test_limits_search_by_user_or_department(self):
        limit1 = UsageLimit.objects.create(user=self.alice, daily_token_cap=1000)
        limit2 = UsageLimit.objects.create(department=self.sales, monthly_token_cap=5000)
        response = self.client.get(reverse("governance:limits"), {"search": "alice"})
        self.assertEqual(list(response.context["limits"]), [limit1])
        response = self.client.get(reverse("governance:limits"), {"search": "sales"})
        self.assertEqual(list(response.context["limits"]), [limit2])

    def test_audit_logs_action_type_filter(self):
        AuditLog.objects.create(actor=self.admin, action_type="user.role_change", target_type="User", target_id="1")
        AuditLog.objects.create(actor=self.admin, action_type="model.enable", target_type="ModelConfig", target_id="1")
        response = self.client.get(reverse("governance:audit_logs"), {"action_type": "model.enable"})
        self.assertEqual(len(response.context["logs"]), 1)
        self.assertEqual(response.context["logs"][0].action_type, "model.enable")

    def test_audit_logs_date_range_filter_excludes_out_of_range(self):
        AuditLog.objects.create(actor=self.admin, action_type="user.role_change", target_type="User", target_id="1")
        response = self.client.get(
            reverse("governance:audit_logs"), {"date_from": "2020-01-01", "date_to": "2020-01-02"}
        )
        self.assertEqual(len(response.context["logs"]), 0)

    def test_usage_search_and_model_filter(self):
        model = ModelConfig.objects.create(provider="openai", model_name="gpt-4o-mini", is_enabled=True)
        conv = Conversation.objects.create(user=self.alice, title="c")
        Message.objects.create(
            conversation=conv,
            role=Message.Role.ASSISTANT,
            content="hi",
            model_used=model,
            input_tokens=10,
            output_tokens=5,
            estimated_cost="0.01",
        )
        response = self.client.get(reverse("governance:usage"), {"search": "alice"})
        self.assertEqual(len(response.context["per_user"]), 1)
        response = self.client.get(reverse("governance:usage"), {"search": "nobody"})
        self.assertEqual(len(response.context["per_user"]), 0)
        response = self.client.get(reverse("governance:usage"), {"model": model.id})
        self.assertEqual(len(response.context["per_user"]), 1)

    def test_filters_are_admin_only(self):
        User.objects.create_user(email="plain@example.com", password="pw12345!")
        self.client.logout()
        self.client.login(email="plain@example.com", password="pw12345!")
        for url_name in ["users", "models", "departments", "limits", "audit_logs", "usage"]:
            response = self.client.get(reverse(f"governance:{url_name}"), {"search": "x"})
            self.assertEqual(response.status_code, 403, url_name)
