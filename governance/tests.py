from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import Department, Team, User
from chat.models import Conversation, Message, ModelConfig, PromptTemplate, UserModelPermission
from chat.prompts import build_system_prompt
from governance.limits import UploadRejected, UsageLimitExceeded, check_usage_limits, validate_upload
from governance.models import (
    ADMIN_NAV_FEATURES,
    AuditLog,
    Plan,
    ROLE_FEATURE_ROLES,
    RoleFeatureToggle,
    SystemPromptVersion,
    USER_CHAT_FEATURES,
    UsageLimit,
)
from governance.plans import (
    assign_plan,
    check_request_count_limit,
    effective_allowed_model_ids,
    validate_context_tokens,
)


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


class PlanRestructureTests(TestCase):
    """The 4-dimension extension: max_requests_per_period/period,
    max_context_tokens, the new feature_flags keys, and the
    restructure_plans management command's rename/dry-run behavior."""

    def setUp(self):
        self.user = User.objects.create_user(email="restructure@example.com", password="pw12345!")
        self.conversation = Conversation.objects.create(user=self.user)

    def _plan(self, **overrides):
        defaults = {"name": "TestPlan", "max_requests_per_period": None, "period": None}
        defaults.update(overrides)
        plan = Plan.objects.create(**defaults)
        assign_plan(self.user, plan)
        return plan

    # ---- max_requests_per_period / period ----

    def test_no_period_configured_is_unrestricted(self):
        self._plan()
        check_request_count_limit(self.user, self.conversation)  # should not raise

    def test_session_period_counts_only_this_conversation(self):
        self._plan(max_requests_per_period=2, period="session")
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="1")
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="2")
        with self.assertRaises(UsageLimitExceeded):
            check_request_count_limit(self.user, self.conversation)

        other_conversation = Conversation.objects.create(user=self.user)
        check_request_count_limit(self.user, other_conversation)  # different session, should not raise

    def test_day_period_counts_across_all_conversations_today(self):
        self._plan(max_requests_per_period=2, period="day")
        other_conversation = Conversation.objects.create(user=self.user)
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="1")
        Message.objects.create(conversation=other_conversation, role=Message.Role.USER, content="2")
        with self.assertRaises(UsageLimitExceeded):
            check_request_count_limit(self.user, self.conversation)

    def test_month_period_allows_when_under(self):
        self._plan(max_requests_per_period=5, period="month")
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="1")
        check_request_count_limit(self.user, self.conversation)  # should not raise

    def test_no_plan_assigned_is_unrestricted(self):
        # No assign_plan() call at all - matches the app-wide rule that an
        # unassigned user is unrestricted, not silently locked out.
        check_request_count_limit(self.user, self.conversation)

    # ---- max_context_tokens ----

    def test_no_context_cap_configured_is_unrestricted(self):
        self._plan()
        validate_context_tokens(self.user, "system prompt", [{"role": "user", "content": "hi"}])

    def test_context_under_cap_allowed(self):
        self._plan(max_context_tokens=1000)
        validate_context_tokens(self.user, "short", [{"role": "user", "content": "also short"}])

    def test_context_over_cap_rejected(self):
        self._plan(max_context_tokens=10)  # ~40 characters allowed
        long_history = [{"role": "user", "content": "x" * 500}]
        with self.assertRaises(UsageLimitExceeded):
            validate_context_tokens(self.user, "system prompt", long_history)

    def test_context_cap_counts_system_prompt_and_full_history(self):
        self._plan(max_context_tokens=50)  # ~200 characters allowed
        history = [{"role": "user", "content": "x" * 90}, {"role": "assistant", "content": "y" * 90}]
        with self.assertRaises(UsageLimitExceeded):
            validate_context_tokens(self.user, "z" * 90, history)

    # ---- feature_flags (new keys reuse the existing dict/mechanism) ----

    def test_new_feature_flag_keys_read_correctly(self):
        plan = self._plan(
            feature_flags={"tools": True, "priority_queue": False, "long_context": True},
        )
        self.assertTrue(plan.has_feature("tools"))
        self.assertFalse(plan.has_feature("priority_queue"))
        self.assertTrue(plan.has_feature("long_context"))

    def test_unset_new_flag_defaults_to_false(self):
        plan = self._plan(feature_flags={})
        self.assertFalse(plan.has_feature("tools"))


class RestructurePlansCommandTests(TestCase):
    """The one-time rename/extend command - dry-run must never write,
    --apply must rename Standard/Premium IN PLACE (same id) so existing
    UserPlanAssignment rows keep resolving, and Full must be created
    fresh."""

    def setUp(self):
        # Standard/Premium/Demo already exist here, seeded by migration
        # 0005_seed_plans (runs automatically for every fresh test DB) -
        # fetch and normalize them rather than creating duplicates, which
        # would violate the unique name constraint.
        self.standard = Plan.objects.get(name="Standard")
        self.standard.feature_flags = {"file_upload": True}
        self.standard.save(update_fields=["feature_flags"])
        self.premium = Plan.objects.get(name="Premium")
        self.premium.feature_flags = {"file_upload": True, "export": True}
        self.premium.save(update_fields=["feature_flags"])
        self.demo = Plan.objects.get(name="Demo")
        self.user_on_standard = User.objects.create_user(email="onstandard@example.com", password="pw12345!")
        assign_plan(self.user_on_standard, self.standard)

    def test_dry_run_writes_nothing(self):
        out = StringIO()
        call_command("restructure_plans", stdout=out)
        self.standard.refresh_from_db()
        self.assertEqual(self.standard.name, "Standard")
        self.assertIsNone(self.standard.max_requests_per_period)
        self.assertFalse(Plan.objects.filter(name="Full").exists())
        self.assertIn("DRY RUN", out.getvalue())

    def test_apply_renames_in_place_preserving_id(self):
        standard_id = self.standard.id
        premium_id = self.premium.id
        call_command("restructure_plans", "--apply", stdout=StringIO())

        renamed_basic = Plan.objects.get(id=standard_id)
        self.assertEqual(renamed_basic.name, "Basic")
        self.assertEqual(renamed_basic.max_requests_per_period, 50)
        self.assertEqual(renamed_basic.period, "day")
        self.assertTrue(renamed_basic.feature_flags["file_upload"])  # existing key preserved
        self.assertFalse(renamed_basic.feature_flags["tools"])  # new key added

        renamed_advanced = Plan.objects.get(id=premium_id)
        self.assertEqual(renamed_advanced.name, "Advanced")
        self.assertEqual(renamed_advanced.max_requests_per_period, 200)
        self.assertTrue(renamed_advanced.feature_flags["export"])  # existing key preserved
        self.assertTrue(renamed_advanced.feature_flags["tools"])  # new key added

    def test_apply_creates_full_plan_fresh(self):
        call_command("restructure_plans", "--apply", stdout=StringIO())
        full = Plan.objects.get(name="Full")
        self.assertEqual(full.max_requests_per_period, 1000)
        self.assertEqual(full.max_context_tokens, 128000)
        self.assertTrue(full.feature_flags["priority_queue"])

    def test_existing_user_assignment_still_resolves_after_rename(self):
        call_command("restructure_plans", "--apply", stdout=StringIO())
        self.user_on_standard.refresh_from_db()
        assignment = self.user_on_standard.plan_assignment
        self.assertEqual(assignment.plan.name, "Basic")
        self.assertEqual(assignment.plan_id, self.standard.id)

    def test_apply_updates_demo_in_place(self):
        call_command("restructure_plans", "--apply", stdout=StringIO())
        self.demo.refresh_from_db()
        self.assertEqual(self.demo.name, "Demo")  # not renamed
        self.assertEqual(self.demo.max_requests_per_period, 10)
        self.assertEqual(self.demo.max_context_tokens, 4000)

    def test_missing_plan_does_not_crash(self):
        # UserPlanAssignment.plan is on_delete=PROTECT (by design - a plan
        # with real users assigned can't be deleted out from under them),
        # so detach the assignment first to simulate "Standard doesn't
        # exist in this environment" without fighting that protection.
        self.user_on_standard.plan_assignment.delete()
        self.standard.delete()
        out = StringIO()
        call_command("restructure_plans", "--apply", stdout=out)
        self.assertIn("not found", out.getvalue())
        # Premium/Demo/Full still process fine despite Standard missing.
        self.assertTrue(Plan.objects.filter(name="Advanced").exists())
        self.assertTrue(Plan.objects.filter(name="Full").exists())


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
        self.department = Department.objects.create(name="Ops")
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            department=self.department,
            is_staff=True,
        )
        self.superadmin = User.objects.create_user(
            email="superadmin@example.com",
            password="pw12345!",
            role=User.Role.SUPERADMIN,
            is_staff=True,
        )
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!", department=self.department)

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
        """A model_name containing "</script>" must never let it inject a real
        <script> tag - the "Cost by model" card renders it server-side via
        Django's normal HTML autoescaping (it's no longer JS-chart-driven)."""
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
        self.assertIn("&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;", body)

    def test_change_user_role_writes_audit_log(self):
        """Promoting to Manager requires a Team (Section 1B) - the request
        must include team_id + confirmed=1 in one shot, or the view just
        re-renders the confirmation partial without applying anything."""
        team = Team.objects.create(name="Alpha", department=self.department)
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_role", kwargs={"user_id": self.user.id}),
            {"role": User.Role.MANAGER, "team_id": team.id, "confirmed": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, User.Role.MANAGER)
        self.assertEqual(self.user.team_id, team.id)
        team.refresh_from_db()
        self.assertEqual(team.manager_id, self.user.id)
        self.assertTrue(AuditLog.objects.filter(action_type="user.role_change").exists())

    def test_change_user_role_to_manager_without_team_does_not_apply(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_role", kwargs={"user_id": self.user.id}),
            {"role": User.Role.MANAGER},
        )
        self.assertEqual(response.status_code, 200)  # re-renders the confirm partial, not a redirect
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, User.Role.USER)

    def test_scoped_admin_cannot_promote_to_admin(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_role", kwargs={"user_id": self.user.id}),
            {"role": User.Role.ADMIN, "department_id": self.department.id, "confirmed": "1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_superadmin_can_promote_to_admin_with_department(self):
        self.client.login(email="superadmin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_role", kwargs={"user_id": self.user.id}),
            {"role": User.Role.ADMIN, "department_id": self.department.id, "confirmed": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, User.Role.ADMIN)
        self.assertEqual(self.user.department_id, self.department.id)

    def test_change_user_department_writes_audit_log(self):
        department = Department.objects.create(name="Support")
        self.client.login(email="superadmin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_department", kwargs={"user_id": self.user.id}),
            {"department_id": department.id},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.department_id, department.id)
        self.assertTrue(AuditLog.objects.filter(action_type="user.department_change").exists())

    def test_change_user_department_to_none(self):
        self.client.login(email="superadmin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_department", kwargs={"user_id": self.user.id}),
            {"department_id": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.department_id)

    def test_change_user_department_forbidden_for_scoped_admin(self):
        department = Department.objects.create(name="Support")
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_department", kwargs={"user_id": self.user.id}),
            {"department_id": department.id},
        )
        self.assertEqual(response.status_code, 403)

    def test_toggle_model_enabled_writes_audit_log(self):
        model_config = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="m",
            is_enabled=False,
        )
        self.client.login(email="superadmin@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_model_enabled", kwargs={"model_id": model_config.id}))
        self.assertEqual(response.status_code, 302)
        model_config.refresh_from_db()
        self.assertTrue(model_config.is_enabled)
        self.assertTrue(AuditLog.objects.filter(action_type="model.enable").exists())

    def test_toggle_model_enabled_forbidden_for_scoped_admin(self):
        model_config = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m")
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_model_enabled", kwargs={"model_id": model_config.id}))
        self.assertEqual(response.status_code, 403)

    def test_system_prompt_new_version_writes_audit_log(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:system_prompt", kwargs={"department_id": self.department.id}),
            {"content": "Be concise.", "tone_preference": "casual", "restricted_topics": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SystemPromptVersion.objects.filter(department=self.department, is_active=True).exists())
        self.assertTrue(AuditLog.objects.filter(action_type="system_prompt.new_version").exists())

    def test_system_prompt_forbidden_for_other_departments_admin(self):
        other_department = Department.objects.create(name="Other")
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:system_prompt", kwargs={"department_id": other_department.id}),
            {"content": "Be concise.", "tone_preference": "casual", "restricted_topics": ""},
        )
        self.assertEqual(response.status_code, 403)

    def test_add_model_creates_disabled_unpriced_entry(self):
        self.client.login(email="superadmin@example.com", password="pw12345!")
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

    def test_add_model_forbidden_for_scoped_admin(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:add_model"),
            {"provider": ModelConfig.Provider.OPENAI, "model_name": "gpt-new", "tier": ModelConfig.Tier.DEFAULT},
        )
        self.assertEqual(response.status_code, 403)

    def test_update_model_pricing(self):
        model_config = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m")
        self.client.login(email="superadmin@example.com", password="pw12345!")
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
        self.client.login(email="superadmin@example.com", password="pw12345!")

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

    def test_model_permissions_forbidden_for_scoped_admin(self):
        model_config = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m", is_enabled=True)
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:model_permissions", kwargs={"model_id": model_config.id}))
        self.assertEqual(response.status_code, 403)


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


class UserOverridesTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
        )
        self.client.login(email="admin@example.com", password="pw12345!")
        self.target = User.objects.create_user(email="target@example.com", password="pw12345!")
        self.model_config = ModelConfig.objects.create(
            provider="openai", model_name="gpt-5.6-sol", display_name="Sol", is_enabled=True
        )

    def test_users_table_shows_override_count(self):
        UsageLimit.objects.create(user=self.target, daily_token_cap=1000)
        response = self.client.get(reverse("governance:users"))
        self.assertContains(response, "1 custom override beyond Plan defaults")

    def test_users_table_hides_override_link_when_none(self):
        response = self.client.get(reverse("governance:users"))
        self.assertNotContains(response, "custom override")

    def test_overrides_page_lists_usage_limit_and_model_permissions(self):
        UsageLimit.objects.create(user=self.target, daily_token_cap=500)
        UserModelPermission.objects.create(user=self.target, model_config=self.model_config, is_allowed=False)
        response = self.client.get(reverse("governance:user_overrides", kwargs={"user_id": self.target.id}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "500")
        self.assertContains(response, "Sol")

    def test_overrides_page_empty_state(self):
        response = self.client.get(reverse("governance:user_overrides", kwargs={"user_id": self.target.id}))
        self.assertContains(response, "No personal usage limit set")
        self.assertContains(response, "No per-model access overrides")

    def test_clear_overrides_removes_usage_limit_and_permissions(self):
        UsageLimit.objects.create(user=self.target, daily_token_cap=500)
        UserModelPermission.objects.create(user=self.target, model_config=self.model_config, is_allowed=False)
        response = self.client.post(reverse("governance:clear_user_overrides", kwargs={"user_id": self.target.id}))
        self.assertRedirects(response, reverse("governance:users"))
        self.assertFalse(UsageLimit.objects.filter(user=self.target).exists())
        self.assertFalse(UserModelPermission.objects.filter(user=self.target).exists())
        self.assertTrue(
            AuditLog.objects.filter(action_type="user.overrides_cleared", target_id=str(self.target.id)).exists()
        )

    def test_clear_overrides_with_nothing_to_clear_does_not_error(self):
        response = self.client.post(reverse("governance:clear_user_overrides", kwargs={"user_id": self.target.id}))
        self.assertRedirects(response, reverse("governance:users"))
        self.assertFalse(AuditLog.objects.filter(action_type="user.overrides_cleared").exists())

    def test_non_admin_cannot_clear_overrides(self):
        self.client.logout()
        self.client.login(email="target@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:clear_user_overrides", kwargs={"user_id": self.target.id}))
        self.assertEqual(response.status_code, 403)

    def test_non_admin_cannot_view_overrides_page(self):
        self.client.logout()
        self.client.login(email="target@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:user_overrides", kwargs={"user_id": self.target.id}))
        self.assertEqual(response.status_code, 403)


class DepartmentManagementTests(TestCase):
    """Department structure (create/rename/delete) is SuperAdmin-only per
    the role hierarchy prompt, Section 1."""

    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.SUPERADMIN,
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
        # SuperAdmin, not a department-scoped Admin: this class tests
        # search/filter/htmx mechanics across departments (alice in
        # Engineering, bob in Sales) - department-SCOPING itself has its
        # own dedicated test class (DepartmentScopingTests) rather than
        # being conflated with these cross-cutting list-screen mechanics.
        self.admin = User.objects.create_user(
            email="filteradmin@example.com",
            password="pw12345!",
            role=User.Role.SUPERADMIN,
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


class ModelSyncTests(TestCase):
    """Sync Models: fetch real provider model IDs instead of hand-typing
    them (see chat/model_sync.py) - the picker/import views live in
    governance, the fetch logic lives in chat since it's provider-specific.
    Provider calls are always mocked here - never hit a real API in tests."""

    def setUp(self):
        # Model sync/enable/pricing is SuperAdmin-only per the role
        # hierarchy prompt, Section 1 ("enable/disable individual models
        # system-wide").
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.SUPERADMIN,
            is_staff=True,
        )
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="admin@example.com", password="pw12345!")

    def test_preview_forbidden_for_non_admin(self):
        self.client.logout()
        self.client.login(email="u@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:sync_models_preview"))
        self.assertEqual(response.status_code, 403)

    def test_preview_shows_no_api_key_state(self):
        from unittest.mock import patch

        with patch("chat.model_sync.settings.OPENAI_API_KEY", ""), patch(
            "chat.model_sync.settings.ANTHROPIC_API_KEY", ""
        ):
            response = self.client.get(reverse("governance:sync_models_preview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No API key configured")

    def test_preview_separates_new_from_already_tracked_models(self):
        from unittest.mock import patch

        ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="gpt-4o-mini",
            tier=ModelConfig.Tier.ECONOMY,
        )
        # Patching the individual fetch_openai_models/fetch_anthropic_models
        # names here would NOT work: chat/model_sync.py's _FETCHERS dict
        # captures those function objects at module-import time, so a
        # later patch of the module-level name doesn't reach callers that
        # go through _FETCHERS. fetch_all_available_models is the one
        # entry point sync_models_preview actually calls (and re-imports
        # locally on every request), so that's the correct patch target.
        with patch(
            "chat.model_sync.fetch_all_available_models",
            return_value={
                ModelConfig.Provider.OPENAI: {
                    "configured": True,
                    "models": ["gpt-4o-mini", "gpt-4o"],
                    "error": None,
                },
                ModelConfig.Provider.ANTHROPIC: {"configured": True, "models": [], "error": None},
            },
        ):
            response = self.client.get(reverse("governance:sync_models_preview"))
        fetched = response.context["fetched"]
        self.assertEqual(fetched[ModelConfig.Provider.OPENAI]["new_models"], ["gpt-4o"])
        self.assertEqual(fetched[ModelConfig.Provider.OPENAI]["already_tracked"], ["gpt-4o-mini"])

    def test_preview_surfaces_fetch_error_without_crashing(self):
        from unittest.mock import patch

        with patch(
            "chat.model_sync.fetch_all_available_models",
            return_value={
                ModelConfig.Provider.OPENAI: {"configured": True, "models": [], "error": "network down"},
                ModelConfig.Provider.ANTHROPIC: {"configured": True, "models": [], "error": None},
            },
        ):
            response = self.client.get(reverse("governance:sync_models_preview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "network down")

    def test_import_creates_disabled_model_with_exact_id(self):
        response = self.client.post(
            reverse("governance:sync_models_import"),
            {
                "model": [f"{ModelConfig.Provider.OPENAI}::gpt-4o"],
                f"tier__{ModelConfig.Provider.OPENAI}::gpt-4o": "premium",
            },
        )
        self.assertRedirects(response, reverse("governance:models"))
        model_config = ModelConfig.objects.get(provider=ModelConfig.Provider.OPENAI, model_name="gpt-4o")
        self.assertFalse(model_config.is_enabled)
        self.assertEqual(model_config.tier, ModelConfig.Tier.PREMIUM)
        self.assertTrue(AuditLog.objects.filter(action_type="model.sync_import").exists())

    def test_import_is_idempotent_for_already_tracked_models(self):
        existing = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="gpt-4o-mini",
            tier=ModelConfig.Tier.ECONOMY,
        )
        self.client.post(
            reverse("governance:sync_models_import"),
            {"model": [f"{ModelConfig.Provider.OPENAI}::gpt-4o-mini"]},
        )
        self.assertEqual(
            ModelConfig.objects.filter(provider=ModelConfig.Provider.OPENAI, model_name="gpt-4o-mini").count(), 1
        )
        existing.refresh_from_db()
        self.assertEqual(existing.tier, ModelConfig.Tier.ECONOMY)  # untouched, not overwritten

    def test_import_ignores_malformed_or_unknown_provider_entries(self):
        response = self.client.post(
            reverse("governance:sync_models_import"),
            {"model": ["not-encoded-properly", "made-up-provider::some-model"]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ModelConfig.objects.count(), 0)

    def test_import_forbidden_for_non_admin(self):
        self.client.logout()
        self.client.login(email="u@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:sync_models_import"),
            {"model": [f"{ModelConfig.Provider.OPENAI}::gpt-4o"]},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ModelConfig.objects.count(), 0)

    def test_display_name_editable_via_pricing_update(self):
        model_config = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="gpt-4o")
        response = self.client.post(
            reverse("governance:update_model_pricing", kwargs={"model_id": model_config.id}),
            {"input_cost_per_1m": "1.5", "output_cost_per_1m": "3", "display_name": "GPT-4o (flagship)"},
        )
        self.assertEqual(response.status_code, 302)
        model_config.refresh_from_db()
        self.assertEqual(model_config.display_name, "GPT-4o (flagship)")
        self.assertEqual(model_config.display_label, "GPT-4o (flagship)")


class ModelSyncFetchHelperTests(TestCase):
    """Unit tests for chat/model_sync.py's own filtering/aggregation logic,
    independent of the admin views above."""

    def test_openai_chat_filter_excludes_known_non_chat_families(self):
        from chat.model_sync import _is_openai_chat_model

        self.assertTrue(_is_openai_chat_model("gpt-4o"))
        self.assertTrue(_is_openai_chat_model("gpt-4o-mini"))
        self.assertFalse(_is_openai_chat_model("text-embedding-3-large"))
        self.assertFalse(_is_openai_chat_model("whisper-1"))
        self.assertFalse(_is_openai_chat_model("tts-1"))
        self.assertFalse(_is_openai_chat_model("dall-e-3"))
        self.assertFalse(_is_openai_chat_model("text-moderation-latest"))

    def test_fetch_all_available_models_reports_unconfigured_provider(self):
        from unittest.mock import patch

        from chat.model_sync import fetch_all_available_models

        with patch("chat.model_sync.settings.OPENAI_API_KEY", ""), patch(
            "chat.model_sync.settings.ANTHROPIC_API_KEY", ""
        ):
            result = fetch_all_available_models()
        self.assertFalse(result[ModelConfig.Provider.OPENAI]["configured"])
        self.assertFalse(result[ModelConfig.Provider.ANTHROPIC]["configured"])

    def test_known_model_keys_reflects_existing_modelconfigs(self):
        from chat.model_sync import known_model_keys

        ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="gpt-4o")
        self.assertIn((ModelConfig.Provider.OPENAI, "gpt-4o"), known_model_keys())
        self.assertNotIn((ModelConfig.Provider.OPENAI, "gpt-4o-mini"), known_model_keys())


class ModelSyncNotificationTaskTests(TestCase):
    """The daily check_for_new_models Celery task (see chat/tasks.py) only
    notifies admins about undiscovered models - it never creates/enables
    anything itself, mirroring what the manual Sync Models button does."""

    def setUp(self):
        # chat/tasks.py now notifies SuperAdmins (model sync/enable is
        # SuperAdmin-only, see ModelSyncTests above).
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.SUPERADMIN,
            is_staff=True,
        )

    def test_notifies_admins_when_new_models_found(self):
        from unittest.mock import patch

        from chat.tasks import check_for_new_models
        from notifications.models import Notification, NotificationType

        with patch(
            "chat.model_sync.fetch_all_available_models",
            return_value={
                ModelConfig.Provider.OPENAI: {"configured": True, "models": ["gpt-4o"], "error": None},
                ModelConfig.Provider.ANTHROPIC: {"configured": True, "models": [], "error": None},
            },
        ):
            check_for_new_models()
        self.assertTrue(
            Notification.objects.filter(
                user=self.admin, notification_type=NotificationType.MODEL_SYNC_AVAILABLE
            ).exists()
        )

    def test_does_not_notify_when_nothing_new(self):
        from unittest.mock import patch

        from chat.tasks import check_for_new_models
        from notifications.models import Notification, NotificationType

        ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="gpt-4o")
        with patch(
            "chat.model_sync.fetch_all_available_models",
            return_value={
                ModelConfig.Provider.OPENAI: {"configured": True, "models": ["gpt-4o"], "error": None},
                ModelConfig.Provider.ANTHROPIC: {"configured": True, "models": [], "error": None},
            },
        ):
            check_for_new_models()
        self.assertFalse(
            Notification.objects.filter(
                user=self.admin, notification_type=NotificationType.MODEL_SYNC_AVAILABLE
            ).exists()
        )

    def test_dedup_prevents_repeat_notification_within_a_day(self):
        from unittest.mock import patch

        from chat.tasks import check_for_new_models
        from notifications.models import Notification, NotificationType

        with patch(
            "chat.model_sync.fetch_all_available_models",
            return_value={
                ModelConfig.Provider.OPENAI: {"configured": True, "models": ["gpt-4o"], "error": None},
                ModelConfig.Provider.ANTHROPIC: {"configured": True, "models": [], "error": None},
            },
        ):
            check_for_new_models()
            check_for_new_models()
        self.assertEqual(
            Notification.objects.filter(
                user=self.admin, notification_type=NotificationType.MODEL_SYNC_AVAILABLE
            ).count(),
            1,
        )


class DepartmentTemplatesAdminTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        # Department content (system prompt, team templates) is scoped, not
        # SuperAdmin-only - the Admin must belong to the SAME department
        # they're managing templates for.
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            department=self.department,
            is_staff=True,
        )
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="admin@example.com", password="pw12345!")

    def test_admin_creates_team_template(self):
        response = self.client.post(
            reverse("governance:department_templates", kwargs={"department_id": self.department.id}),
            {"name": "Weekly update", "content": "Summarize this week's progress"},
        )
        self.assertRedirects(
            response, reverse("governance:department_templates", kwargs={"department_id": self.department.id})
        )
        template = PromptTemplate.objects.get(name="Weekly update")
        self.assertEqual(template.department, self.department)
        self.assertIsNone(template.owner)
        self.assertTrue(template.is_team_template)
        self.assertTrue(AuditLog.objects.filter(action_type="prompt_template.create").exists())

    def test_admin_deletes_team_template(self):
        template = PromptTemplate.objects.create(department=self.department, name="Old", content="x")
        response = self.client.post(
            reverse(
                "governance:delete_department_template",
                kwargs={"department_id": self.department.id, "template_id": template.id},
            )
        )
        self.assertRedirects(
            response, reverse("governance:department_templates", kwargs={"department_id": self.department.id})
        )
        self.assertFalse(PromptTemplate.objects.filter(id=template.id).exists())
        self.assertTrue(AuditLog.objects.filter(action_type="prompt_template.delete").exists())

    def test_department_templates_forbidden_for_non_admin(self):
        self.client.logout()
        self.client.login(email="u@example.com", password="pw12345!")
        response = self.client.get(
            reverse("governance:department_templates", kwargs={"department_id": self.department.id})
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_team_template_forbidden_for_non_admin(self):
        template = PromptTemplate.objects.create(department=self.department, name="Old", content="x")
        self.client.logout()
        self.client.login(email="u@example.com", password="pw12345!")
        response = self.client.post(
            reverse(
                "governance:delete_department_template",
                kwargs={"department_id": self.department.id, "template_id": template.id},
            )
        )
        self.assertEqual(response.status_code, 403)


class RoleHierarchyAccessControlTests(TestCase):
    """Real attacker-style checks for the role hierarchy prompt's Section 4
    ("Enforcement — server-side, not just hidden UI") - every scoping rule
    is exercised as a direct request against another department's data or
    a write endpoint someone shouldn't reach, not just confirmed absent
    from a menu. This is the deliverable's required evidence, not a
    regression-test afterthought."""

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin_a = User.objects.create_user(
            email="admina@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            department=self.dept_a,
            is_staff=True,
        )
        self.admin_b = User.objects.create_user(
            email="adminb@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            department=self.dept_b,
            is_staff=True,
        )
        self.user_a = User.objects.create_user(email="usera@example.com", password="pw12345!", department=self.dept_a)
        self.user_b = User.objects.create_user(email="userb@example.com", password="pw12345!", department=self.dept_b)

        self.team_a = Team.objects.create(name="Team A", department=self.dept_a)
        self.manager_a = User.objects.create_user(
            email="managera@example.com",
            password="pw12345!",
            role=User.Role.MANAGER,
            department=self.dept_a,
            team=self.team_a,
        )
        self.team_a.manager = self.manager_a
        self.team_a.save(update_fields=["manager"])

        self.plain_user = User.objects.create_user(email="plain@example.com", password="pw12345!")

    # --- A department Admin cannot fetch/view/modify another department's
    # users/usage/audit logs by directly hitting a URL with another
    # department's id. ---

    def test_admin_users_list_excludes_another_department(self):
        self.client.login(email="admina@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:users"))
        users = list(response.context["users"])
        self.assertIn(self.user_a, users)
        self.assertNotIn(self.user_b, users)

    def test_admin_cannot_toggle_another_departments_user(self):
        self.client.login(email="admina@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_user_active", kwargs={"user_id": self.user_b.id}))
        self.assertEqual(response.status_code, 403)
        self.user_b.refresh_from_db()
        self.assertTrue(self.user_b.is_active)

    def test_admin_cannot_change_another_departments_user_role(self):
        self.client.login(email="admina@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_role", kwargs={"user_id": self.user_b.id}),
            {"role": User.Role.MANAGER, "confirmed": "1"},
        )
        self.assertEqual(response.status_code, 403)
        self.user_b.refresh_from_db()
        self.assertEqual(self.user_b.role, User.Role.USER)

    def test_admin_cannot_change_another_departments_user_plan(self):
        plan = Plan.objects.create(name="Test Plan A")
        self.client.login(email="admina@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_plan", kwargs={"user_id": self.user_b.id}),
            {"plan_id": plan.id},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_view_another_departments_user_overrides(self):
        self.client.login(email="admina@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:user_overrides", kwargs={"user_id": self.user_b.id}))
        self.assertEqual(response.status_code, 403)

    def test_admin_audit_logs_exclude_another_departments_entries(self):
        from governance.audit import log_action

        log_action(self.admin_a, "user.suspend", self.user_a, old_value=True, new_value=False)
        log_action(self.admin_b, "user.suspend", self.user_b, old_value=True, new_value=False)
        self.client.login(email="admina@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:audit_logs"))
        logs = list(response.context["logs"])
        self.assertTrue(any(entry.actor_id == self.admin_a.id for entry in logs))
        self.assertFalse(any(entry.actor_id == self.admin_b.id for entry in logs))

    def test_admin_usage_export_excludes_another_departments_data(self):
        model = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m", is_enabled=True)
        conv_a = Conversation.objects.create(user=self.user_a, title="a")
        conv_b = Conversation.objects.create(user=self.user_b, title="b")
        Message.objects.create(
            conversation=conv_a,
            role=Message.Role.ASSISTANT,
            content="x",
            model_used=model,
            input_tokens=10,
            output_tokens=5,
            estimated_cost="0.01",
        )
        Message.objects.create(
            conversation=conv_b,
            role=Message.Role.ASSISTANT,
            content="y",
            model_used=model,
            input_tokens=20,
            output_tokens=10,
            estimated_cost="0.02",
        )
        self.client.login(email="admina@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:export_usage_csv"))
        body = response.content.decode()
        self.assertIn("usera@example.com", body)
        self.assertNotIn("userb@example.com", body)

    def test_admin_limits_exclude_another_departments_entries(self):
        UsageLimit.objects.create(user=self.user_a, daily_token_cap=1000)
        limit_b = UsageLimit.objects.create(user=self.user_b, daily_token_cap=2000)
        self.client.login(email="admina@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:limits"))
        limits = list(response.context["limits"])
        self.assertTrue(any(entry.user_id == self.user_a.id for entry in limits))
        self.assertFalse(any(entry.user_id == self.user_b.id for entry in limits))

        response = self.client.post(reverse("governance:limit_delete", kwargs={"limit_id": limit_b.id}))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(UsageLimit.objects.filter(id=limit_b.id).exists())

    def test_admin_cannot_view_another_departments_system_prompt(self):
        self.client.login(email="admina@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:system_prompt", kwargs={"department_id": self.dept_b.id}))
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_manage_another_departments_team(self):
        team_b = Team.objects.create(name="Team B", department=self.dept_b)
        self.client.login(email="admina@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:delete_team", kwargs={"team_id": team_b.id}))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Team.objects.filter(id=team_b.id).exists())

    # --- SuperAdmin-only screens return 403 for Admin and Manager, not
    # just hidden from their navigation. ---

    def test_plan_management_403_for_admin(self):
        self.client.login(email="admina@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("governance:plans")).status_code, 403)

    def test_plan_management_403_for_manager(self):
        self.client.login(email="managera@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("governance:plans")).status_code, 403)

    def test_plan_management_200_for_superadmin(self):
        self.client.login(email="super@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("governance:plans")).status_code, 200)

    def test_departments_403_for_admin(self):
        self.client.login(email="admina@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("governance:departments")).status_code, 403)

    def test_models_403_for_manager(self):
        self.client.login(email="managera@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("governance:models")).status_code, 403)

    # --- A Manager cannot successfully call any write endpoint, even
    # knowing the URL - confirmed as a 403, not just "no button exists". ---

    def test_manager_cannot_toggle_user_active(self):
        self.client.login(email="managera@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_user_active", kwargs={"user_id": self.user_a.id}))
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_change_role(self):
        self.client.login(email="managera@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_role", kwargs={"user_id": self.user_a.id}),
            {"role": User.Role.ADMIN},
        )
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_change_plan(self):
        plan = Plan.objects.create(name="Test Plan M")
        self.client.login(email="managera@example.com", password="pw12345!")
        response = self.client.post(
            reverse("governance:change_user_plan", kwargs={"user_id": self.user_a.id}),
            {"plan_id": plan.id},
        )
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_enable_model(self):
        model = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m")
        self.client.login(email="managera@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_model_enabled", kwargs={"model_id": model.id}))
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_add_department(self):
        self.client.login(email="managera@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:add_department"), {"name": "Sneaky"})
        self.assertEqual(response.status_code, 403)

    def test_manager_dashboard_shows_only_own_team(self):
        model = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m", is_enabled=True)
        conv_a = Conversation.objects.create(user=self.user_a, title="a")
        conv_b = Conversation.objects.create(user=self.user_b, title="b")
        Message.objects.create(
            conversation=conv_a,
            role=Message.Role.ASSISTANT,
            content="x",
            model_used=model,
            input_tokens=100,
            output_tokens=50,
            estimated_cost="0.1",
        )
        Message.objects.create(
            conversation=conv_b,
            role=Message.Role.ASSISTANT,
            content="y",
            model_used=model,
            input_tokens=200,
            output_tokens=100,
            estimated_cost="0.2",
        )
        self.user_a.team = self.team_a
        self.user_a.save(update_fields=["team"])
        self.client.login(email="managera@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:manager_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_tokens_all_time"], 150)

    def test_manager_dashboard_forbidden_for_regular_user(self):
        self.client.login(email="usera@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:manager_dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_manager_can_toggle_own_team_model(self):
        model = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m", is_enabled=True)
        self.client.login(email="managera@example.com", password="pw12345!")

        response = self.client.post(reverse("governance:toggle_team_model", kwargs={"model_id": model.id}))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.team_a.disabled_models.filter(id=model.id).exists())

        # Toggling again re-allows it.
        response = self.client.post(reverse("governance:toggle_team_model", kwargs={"model_id": model.id}))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.team_a.disabled_models.filter(id=model.id).exists())

    def test_manager_cannot_toggle_a_disabled_model(self):
        model = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="off", is_enabled=False)
        self.client.login(email="managera@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_team_model", kwargs={"model_id": model.id}))
        self.assertEqual(response.status_code, 404)

    def test_manager_with_no_team_cannot_toggle(self):
        ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m2", is_enabled=True)
        model = ModelConfig.objects.get(model_name="m2")
        User.objects.create_user(
            email="lonemanager@example.com", password="pw12345!", role=User.Role.MANAGER, department=self.dept_a
        )
        self.client.login(email="lonemanager@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_team_model", kwargs={"model_id": model.id}))
        self.assertEqual(response.status_code, 403)

    def test_regular_user_cannot_toggle_team_model(self):
        model = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m3", is_enabled=True)
        self.client.login(email="usera@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_team_model", kwargs={"model_id": model.id}))
        self.assertEqual(response.status_code, 403)

    def test_team_disabled_model_narrows_effective_allowed_ids(self):
        model = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m4", is_enabled=True)
        plan = Plan.objects.create(name="Team Plan")
        plan.allowed_models.add(model)
        self.user_a.team = self.team_a
        self.user_a.save(update_fields=["team"])
        assign_plan(self.user_a, plan)

        self.assertIn(model.id, effective_allowed_model_ids(self.user_a))

        self.team_a.disabled_models.add(model)
        self.assertNotIn(model.id, effective_allowed_model_ids(self.user_a))

    def test_personal_override_wins_over_team_disabled_model(self):
        model = ModelConfig.objects.create(provider=ModelConfig.Provider.OPENAI, model_name="m5", is_enabled=True)
        plan = Plan.objects.create(name="Team Plan 2")
        plan.allowed_models.add(model)
        self.user_a.team = self.team_a
        self.user_a.save(update_fields=["team"])
        assign_plan(self.user_a, plan)
        self.team_a.disabled_models.add(model)

        UserModelPermission.objects.create(user=self.user_a, model_config=model, is_allowed=True)
        self.assertIn(model.id, effective_allowed_model_ids(self.user_a))

    # --- A regular User still cannot access any Admin/Manager/SuperAdmin
    # endpoint - re-run to confirm the new roles didn't loosen anything. ---

    def test_user_cannot_access_any_governance_screen(self):
        self.client.login(email="plain@example.com", password="pw12345!")
        for url_name in [
            "dashboard",
            "users",
            "plans",
            "models",
            "departments",
            "teams",
            "limits",
            "audit_logs",
            "usage",
            "feedback",
            "manager_dashboard",
        ]:
            response = self.client.get(reverse(f"governance:{url_name}"))
            self.assertEqual(response.status_code, 403, url_name)

    def test_user_cannot_call_any_write_endpoint(self):
        self.client.login(email="plain@example.com", password="pw12345!")
        response = self.client.post(reverse("governance:toggle_user_active", kwargs={"user_id": self.user_a.id}))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            reverse("governance:add_model"), {"provider": "openai", "model_name": "x", "tier": "default"}
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse("governance:add_department"), {"name": "Nope"})
        self.assertEqual(response.status_code, 403)


class FeatureVisibilityTests(TestCase):
    """SuperAdmin-controlled per-role feature switches (see
    governance/models.py's ADMIN_NAV_FEATURES/USER_CHAT_FEATURES and
    governance/features.py) - real requests, not just checking the
    RoleFeatureToggle rows got written."""

    def setUp(self):
        self.department = Department.objects.create(name="Ops")
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            department=self.department,
            is_staff=True,
        )
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")

    def test_feature_visibility_page_superadmin_only(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("governance:feature_visibility")).status_code, 403)
        self.client.logout()
        self.client.login(email="u@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("governance:feature_visibility")).status_code, 403)
        self.client.logout()
        self.client.login(email="super@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("governance:feature_visibility")).status_code, 200)

    def test_everything_visible_by_default(self):
        """No RoleFeatureToggle rows at all yet - nothing should be hidden."""
        from governance.features import role_has_feature

        self.assertTrue(role_has_feature(User.Role.ADMIN, "teams"))
        self.assertTrue(role_has_feature(User.Role.USER, "dark_mode"))
        self.assertTrue(role_has_feature(User.Role.MANAGER, "notifications"))

    def test_superadmin_disables_teams_for_admin_role(self):
        self.client.login(email="super@example.com", password="pw12345!")
        post_data = {}
        for key, _label in ADMIN_NAV_FEATURES:
            if key != "teams":
                post_data[f"toggle_{key}_admin"] = "on"
        for key, _label in USER_CHAT_FEATURES:
            for role in ROLE_FEATURE_ROLES:
                post_data[f"toggle_{key}_{role}"] = "on"
        response = self.client.post(reverse("governance:feature_visibility"), post_data)
        self.assertEqual(response.status_code, 302)

        toggle = RoleFeatureToggle.objects.get(role="admin", feature_key="teams")
        self.assertFalse(toggle.is_enabled)

        # Now the department-scoped Admin is really blocked, server-side.
        self.client.logout()
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:teams"))
        self.assertEqual(response.status_code, 403)

        # ...but a SuperAdmin is never affected by a role toggle.
        self.client.logout()
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:teams"))
        self.assertEqual(response.status_code, 200)

    def test_superadmin_disables_dark_mode_for_user_role(self):
        RoleFeatureToggle.objects.create(role="user", feature_key="dark_mode", is_enabled=False)
        self.client.login(email="u@example.com", password="pw12345!")
        response = self.client.post(reverse("accounts:set_theme_preference"), {"theme": "dark"})
        self.assertEqual(response.status_code, 403)
        self.user.refresh_from_db()
        self.assertEqual(self.user.theme_preference, "system")

    def test_disabling_a_user_feature_does_not_affect_admin_role(self):
        RoleFeatureToggle.objects.create(role="user", feature_key="prompt_templates", is_enabled=False)
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("chat:prompt_template_list"))
        self.assertEqual(response.status_code, 200)

    def test_feature_visibility_page_reflects_saved_state(self):
        RoleFeatureToggle.objects.create(role="admin", feature_key="limits", is_enabled=False)
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("governance:feature_visibility"))
        admin_rows = {row["key"]: row["enabled"] for row in response.context["admin_rows"]}
        self.assertFalse(admin_rows["limits"])
        self.assertTrue(admin_rows["teams"])
