"""Role-based dashboards: SuperAdmin, Admin, Manager and User (governance/dashboards.py + templates/dashboards/).

What matters here is not that a card renders but that each role sees the right things and only those: real numbers
(never invented), one item per underlying problem, scope that matches the pages the cards link to, and no private
content or infrastructure detail in front of someone who is not allowed to see it."""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Team, User
from billing.models import Invoice
from chat.models import Conversation, Message, Project
from governance import dashboards, maintenance
from governance.models import AuditLog, Plan, RoleFeatureToggle, SiteBranding, UpgradeRequest, UsageLimit
from governance.system_status import build_system_status
from notifications.models import Notification, NotificationType
from providers.models import Provider

PASSWORD = "pw12345!Strong"
INFRA_WORDS = ("Redis", "Celery", "PostgreSQL", "System status", "Server health", "Backup", "Background jobs")


def make(email, role=User.Role.USER, **extra):
    return User.objects.create_user(email=email, password=PASSWORD, role=role, **extra)


def reply(user, count=1, title="Chat"):
    conversation = Conversation.objects.create(user=user, title=title)
    for _ in range(count):
        Message.objects.create(
            conversation=conversation, role=Message.Role.ASSISTANT, content="x", input_tokens=5, output_tokens=5
        )
    return conversation


def invoice(recipient, department, status=Invoice.Status.UNPAID, days_until_due=10):
    plan = Plan.objects.first() or Plan.objects.create(name="Growth")
    today = timezone.localdate()
    return Invoice.objects.create(
        department=department,
        recipient_user=recipient,
        plan=plan,
        issue_date=today - timedelta(days=20),
        due_date=today + timedelta(days=days_until_due),
        currency="USD",
        subtotal=Decimal("50"),
        tax_rate=Decimal("0"),
        tax_amount=Decimal("0"),
        total=Decimal("50"),
        status=status,
    )


class DashboardTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.dept_a = Department.objects.create(name="Engineering")
        self.dept_b = Department.objects.create(name="Sales")
        self.superadmin = make("root@corp.io", User.Role.SUPERADMIN)
        self.admin = make("admin.a@corp.io", User.Role.ADMIN, department=self.dept_a)
        self.admin_b = make("admin.b@corp.io", User.Role.ADMIN, department=self.dept_b)
        self.team = Team.objects.create(name="Platform", department=self.dept_a)
        self.manager = make("mgr@corp.io", User.Role.MANAGER, department=self.dept_a)
        self.team.manager = self.manager
        self.team.save()
        self.member = make("sara@corp.io", department=self.dept_a, team=self.team, first_name="Sara")
        self.user = make("ahmed.khan@corp.io", department=self.dept_a)
        self.outsider = make("bilal@corp.io", department=self.dept_b)

    def get(self, who, url_name="accounts:dashboard"):
        self.client.force_login(who)
        return self.client.get(reverse(url_name))

    def tile(self, response, key):
        return next(t for t in response.context["dash"]["overview"] if t["key"] == key)

    def attention_keys(self, response):
        return [item["key"] for item in response.context["dash"]["attention"]]


# ---------------------------------------------------------------------------------------------------------------------
class SuperAdminDashboardTests(DashboardTestCase):
    def test_lands_on_the_superadmin_dashboard_with_the_eight_overview_tiles(self):
        response = self.get(self.superadmin)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-dashboard="superadmin"')
        keys = [t["key"] for t in response.context["dash"]["overview"]]
        self.assertEqual(
            keys, ["system", "providers", "users", "requests", "storage", "invoices", "maintenance", "alerts"]
        )

    def test_the_numbers_are_the_real_ones(self):
        User.objects.filter(pk=self.outsider.pk).update(is_active=False)
        reply(self.user, count=3)
        reply(self.member, count=2)
        response = self.get(self.superadmin)
        self.assertEqual(self.tile(response, "users")["value"], User.objects.filter(is_active=True).count())
        self.assertEqual(self.tile(response, "requests")["value"], 5)
        business = response.context["dash"]["business"]
        self.assertEqual(business["users"]["total"], User.objects.count())
        self.assertEqual(business["conversations"]["total"], 2)

    def test_an_unmeasurable_number_says_unavailable_not_zero(self):
        status = build_status_without_disk()
        request = type("R", (), {"user": self.superadmin})()
        dash = dashboards.superadmin_dashboard(request, status, {}, 0)
        storage = next(t for t in dash["overview"] if t["key"] == "storage")
        self.assertFalse(storage["available"])
        self.assertEqual(storage["value"], "Unavailable")
        self.assertEqual(storage["tone"], "muted")
        self.assertNotIn("Unavailable", [t["value"] for t in dash["overview"] if t["available"]])

    def test_zero_is_shown_when_zero_was_measured(self):
        response = self.get(self.superadmin)
        tile = self.tile(response, "requests")
        self.assertTrue(tile["available"])
        self.assertEqual(tile["value"], 0)

    def test_system_summary_comes_from_system_status(self):
        response = self.get(self.superadmin)
        self.assertEqual(self.tile(response, "system")["value"], "Healthy")
        with patch("governance.views.build_system_status") as build:
            status = build_system_status()
            status["database"]["state"] = "unavailable"
            build.return_value = status
            response = self.get(self.superadmin)
        self.assertEqual(self.tile(response, "system")["tone"], "danger")
        self.assertIn("db", self.attention_keys(response))

    @override_settings(BACKUP_S3_BUCKET="")
    def test_needs_attention_lists_an_unconfigured_backup_once(self):
        response = self.get(self.superadmin)
        self.assertEqual(self.attention_keys(response).count("backup"), 1)
        self.assertContains(response, "Database backup is not configured.")

    @override_settings(BACKUP_S3_BUCKET="portal-backups")
    def test_a_configured_backup_is_not_flagged(self):
        self.assertNotIn("backup", self.attention_keys(self.get(self.superadmin)))

    def test_billing_items_link_to_the_existing_pages_and_do_not_double_count(self):
        invoice(self.user, self.dept_a, Invoice.Status.PENDING_VERIFICATION)
        invoice(self.outsider, self.dept_b, Invoice.Status.PENDING_VERIFICATION)
        invoice(self.user, self.dept_a, Invoice.Status.UNPAID, days_until_due=-3)  # overdue
        response = self.get(self.superadmin)
        items = {i["key"]: i for i in response.context["dash"]["attention"]}
        self.assertIn("2 payment proofs to verify.", items["verification"]["text"])
        self.assertEqual(items["verification"]["url"], reverse("billing:invoices"))
        self.assertIn("1 overdue invoice.", items["overdue"]["text"])
        # the payment-proof notification the same problem produces is not listed a second time
        notify_admin(self.superadmin, NotificationType.INVOICE_PAYMENT_SUBMITTED, "Payment proof submitted")
        response = self.get(self.superadmin)
        notes = response.context["dash"]["notifications"]
        self.assertEqual(notes["unread"], 1)
        self.assertEqual(notes["items"], [])

    def test_a_failing_provider_becomes_one_item_linking_to_providers(self):
        Provider.objects.create(
            name="Acme AI",
            slug="acme",
            adapter_type="openai_compatible",
            is_connected=True,
            last_sync_status=Provider.SyncStatus.FAILED,
            last_sync_error="401 unauthorized sk-should-never-appear",
        )
        response = self.get(self.superadmin)
        item = next(i for i in response.context["dash"]["attention"] if i["key"] == "provider:acme")
        self.assertEqual(item["url"], reverse("providers:list"))
        self.assertNotIn("sk-should-never-appear", response.content.decode())
        self.assertEqual(self.tile(response, "providers")["tone"], "warn")

    def test_attention_items_never_repeat_a_key(self):
        invoice(self.user, self.dept_a, Invoice.Status.PENDING_VERIFICATION)
        keys = self.attention_keys(self.get(self.superadmin))
        self.assertEqual(len(keys), len(set(keys)))
        attention = dashboards.Attention()
        attention.add("k", "warn", "A", "first", "/x/", "Go")
        attention.add("k", "danger", "A", "second", "/y/", "Go")
        self.assertEqual(len(attention.items), 1)
        self.assertEqual(attention.items[0]["text"], "first")

    def test_attention_is_ordered_most_severe_first(self):
        attention = dashboards.Attention()
        attention.add("i", "info", "A", "t", "/", "Go")
        attention.add("d", "danger", "A", "t", "/", "Go")
        attention.add("w", "warn", "A", "t", "/", "Go")
        self.assertEqual([i["key"] for i in attention.sorted()], ["d", "w", "i"])

    def test_maintenance_shows_as_a_tile_and_an_item(self):
        self.assertEqual(self.tile(self.get(self.superadmin), "maintenance")["value"], "None")
        maintenance.schedule(
            self.superadmin,
            "Upgrade",
            "",
            timezone.now() + timedelta(hours=2),
            timezone.now() + timedelta(hours=3),
            notify_users=False,
        )
        response = self.get(self.superadmin)
        self.assertEqual(self.tile(response, "maintenance")["value"], "Scheduled")
        self.assertIn("maintenance", self.attention_keys(response))

    def test_all_clear_message_when_nothing_needs_attention(self):
        with override_settings(BACKUP_S3_BUCKET="portal-backups"):
            response = self.get(self.superadmin)
        self.assertEqual(response.context["dash"]["attention"], [])
        self.assertContains(response, "All critical systems are operational.")
        self.assertEqual(self.tile(response, "alerts")["value"], 0)

    def test_ai_usage_counts_and_breakdown(self):
        reply(self.user, count=4)
        usage = self.get(self.superadmin).context["dash"]["ai_usage"]
        self.assertEqual((usage["today"], usage["week"], usage["month"]), (4, 4, 4))
        self.assertIsNone(usage["trend"], "no trend without a real baseline")

    def test_trend_is_computed_only_against_the_same_hours_of_yesterday(self):
        fixed = datetime(2026, 9, 21, 15, 0, tzinfo=dt_timezone.utc)
        conversation = Conversation.objects.create(user=self.user, title="c")
        for hour, count in ((9, 12), (16, 20)):  # yesterday 09:00 is inside the window, yesterday 16:00 is not
            for _ in range(count):
                message = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="x")
                Message.objects.filter(pk=message.pk).update(
                    created_at=datetime(2026, 9, 20, hour, 0, tzinfo=dt_timezone.utc)
                )
        for _ in range(18):  # today, before 15:00
            message = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="x")
            Message.objects.filter(pk=message.pk).update(created_at=datetime(2026, 9, 21, 8, 0, tzinfo=dt_timezone.utc))
        with patch("django.utils.timezone.now", return_value=fixed):
            usage = dashboards.ai_usage(Message.objects.filter(role=Message.Role.ASSISTANT))
        self.assertEqual(usage["today"], 18)
        self.assertEqual(usage["trend"], {"pct": 50, "direction": "up"})  # 18 vs the 12 of yesterday-so-far

    def test_recent_activity_is_readable_scoped_and_leaks_no_values(self):
        AuditLog.objects.create(
            actor=self.superadmin,
            action_type="user.suspend",
            target_type="User",
            target_id="1",
            new_value="secret@leak.io",
        )
        AuditLog.objects.create(
            actor=self.superadmin, action_type="auth.login", target_type="User", target_id="1", new_value="ip=10.0.0.1"
        )
        AuditLog.objects.create(
            actor=self.user, action_type="conversation.delete", target_type="Conversation", target_id="9"
        )
        response = self.get(self.superadmin)
        texts = [event["text"] for event in response.context["dash"]["activity"]]
        self.assertIn("User suspended", texts)
        self.assertEqual(len(texts), 1, "routine sign-ins and other people's conversation housekeeping are left out")
        self.assertNotIn("secret@leak.io", response.content.decode())
        self.assertNotIn("10.0.0.1", response.content.decode())

    def test_quick_actions_are_the_superadmin_set_and_use_existing_routes(self):
        actions = {a["key"]: a["url"] for a in self.get(self.superadmin).context["dash"]["quick_actions"]}
        self.assertEqual(
            list(actions),
            ["users", "project", "invoices", "providers", "media", "status", "maintenance", "branding", "audit"],
        )
        self.assertEqual(actions["maintenance"], reverse("governance:maintenance"))
        self.assertEqual(actions["invoices"], reverse("billing:invoices"))

    def test_continue_managing_comes_from_the_viewers_own_last_action(self):
        self.assertIsNone(self.get(self.superadmin).context["dash"]["continue_item"])
        AuditLog.objects.create(
            actor=self.superadmin, action_type="provider.resync", target_type="Provider", target_id="1"
        )
        item = self.get(self.superadmin).context["dash"]["continue_item"]
        self.assertEqual((item["title"], item["url"]), ("Provider configuration", reverse("providers:list")))

    def test_the_existing_usage_and_cost_sections_and_system_status_are_still_there(self):
        response = self.get(self.superadmin)
        self.assertContains(response, "Usage & cost")
        self.assertContains(response, "sys-status-title")

    def test_renders_under_every_branding_without_hard_coded_colours(self):
        for preset in ("branding_1", "branding_2", "branding_3"):
            SiteBranding.objects.update_or_create(pk=1, defaults={"preset": preset, "version": 900 + len(preset)})
            cache.clear()
            self.assertContains(self.get(self.superadmin), f'data-brand="{preset}"')
        css = dashboard_css()
        import re

        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css), [], "dashboard CSS uses tokens only")


def build_status_without_disk():
    status = build_system_status()
    for metric in status["server_health"]["metrics"]:
        if metric["key"] == "disk":
            metric.update({"available": False, "state": "unavailable", "value": ""})
    return status


def dashboard_css():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "static" / "css" / "main.css").read_text(encoding="utf-8")
    return text[text.index("Role dashboards (SuperAdmin") :]


def notify_admin(user, notification_type, title):
    return Notification.objects.create(user=user, notification_type=notification_type, title=title)


# ---------------------------------------------------------------------------------------------------------------------
class AdminDashboardTests(DashboardTestCase):
    def test_lands_on_the_admin_dashboard_and_shows_no_infrastructure(self):
        response = self.get(self.admin)
        self.assertContains(response, 'data-dashboard="admin"')
        self.assertEqual(response.context["dash_role"], "admin")
        for word in INFRA_WORDS:
            self.assertNotContains(response, word)
        self.assertNotIn("system", [t["key"] for t in response.context["dash"]["overview"]])

    def test_only_their_departments_numbers(self):
        invoice(self.user, self.dept_a, Invoice.Status.PENDING_VERIFICATION)
        invoice(self.outsider, self.dept_b, Invoice.Status.PENDING_VERIFICATION)
        invoice(self.outsider, self.dept_b, Invoice.Status.PENDING_VERIFICATION)
        reply(self.user, count=2)
        reply(self.outsider, count=7)
        response = self.get(self.admin)
        dash = response.context["dash"]
        self.assertEqual(response.context["dash"]["ai_usage"]["today"], 2)
        self.assertEqual(dash["team"]["members"], User.objects.filter(department=self.dept_a).count())
        item = next(i for i in dash["attention"] if i["key"] == "verification")
        self.assertIn("1 payment proof to verify.", item["text"])

    def test_activity_is_scoped_and_needs_the_audit_logs_feature(self):
        AuditLog.objects.create(
            actor=self.user, action_type="user.plan_change", target_type="User", target_id=str(self.user.pk)
        )
        AuditLog.objects.create(
            actor=self.outsider, action_type="user.suspend", target_type="User", target_id=str(self.outsider.pk)
        )
        texts = [e["text"] for e in self.get(self.admin).context["dash"]["activity"]]
        self.assertEqual(texts, ["Plan changed"])
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.ADMIN, feature_key="audit_logs", defaults={"is_enabled": False}
        )
        self.assertEqual(self.get(self.admin).context["dash"]["activity"], [])

    def test_pending_upgrade_requests_need_the_feature_and_stay_in_the_department(self):
        UpgradeRequest.objects.create(user=self.user, status=UpgradeRequest.Status.PENDING)
        UpgradeRequest.objects.create(user=self.outsider, status=UpgradeRequest.Status.PENDING)
        item = next(i for i in self.get(self.admin).context["dash"]["attention"] if i["key"] == "upgrades")
        self.assertIn("1 plan request waiting.", item["text"])
        self.assertEqual(item["url"], reverse("governance:upgrade_requests"))

    def test_quick_actions_follow_the_role_feature_toggles(self):
        keys = [a["key"] for a in self.get(self.admin).context["dash"]["quick_actions"]]
        self.assertEqual(keys, ["project", "conversation", "invite", "projects", "notifications", "billing"])
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.ADMIN, feature_key="projects", defaults={"is_enabled": False}
        )
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.ADMIN, feature_key="notifications", defaults={"is_enabled": False}
        )
        keys = [a["key"] for a in self.get(self.admin).context["dash"]["quick_actions"]]
        self.assertEqual(keys, ["conversation", "invite", "billing"])

    def test_team_overview_shows_counts_only_never_project_names(self):
        project = Project.objects.create(user=self.user, name="PRIVATE-PROJECT-NAME")
        conversation = Conversation.objects.create(user=self.user, title="PRIVATE-CHAT-TITLE", project=project)
        self.assertTrue(conversation.pk)
        response = self.get(self.admin)
        self.assertEqual(response.context["dash"]["projects"]["total"], 1)
        self.assertNotContains(response, "PRIVATE-PROJECT-NAME")
        self.assertNotContains(response, "PRIVATE-CHAT-TITLE")

    def test_the_setup_checklist_moved_with_the_admin_home(self):
        self.assertContains(self.get(self.admin), "Finish setting up your department")

    def test_an_admin_without_a_department_sees_nothing_of_anyone_elses(self):
        loner = make("loner@corp.io", User.Role.ADMIN)
        reply(self.user, count=3)
        response = self.get(loner)
        self.assertEqual(response.context["dash"]["ai_usage"]["today"], 0)
        self.assertEqual(response.context["dash"]["team"]["members"], 0)


# ---------------------------------------------------------------------------------------------------------------------
class ManagerDashboardTests(DashboardTestCase):
    def test_lands_on_the_manager_dashboard_with_their_team_only(self):
        reply(self.member, count=2)
        reply(self.outsider, count=9)
        response = self.get(self.manager)
        self.assertContains(response, 'data-dashboard="manager"')
        self.assertEqual(self.tile(response, "members")["value"], 1)
        self.assertEqual(response.context["dash"]["ai_usage"]["today"], 2)
        for word in INFRA_WORDS:
            self.assertNotContains(response, word)

    def test_members_private_titles_and_project_names_are_never_shown(self):
        project = Project.objects.create(user=self.member, name="SECRET-PROJECT")
        Conversation.objects.create(user=self.member, title="SECRET-TITLE", project=project)
        response = self.get(self.manager, "governance:manager_dashboard")
        self.assertNotContains(response, "SECRET-PROJECT")
        self.assertNotContains(response, "SECRET-TITLE")
        events = response.context["dash"]["activity"]
        self.assertEqual({e["text"] for e in events}, {"started a conversation", "created a project"})
        self.assertTrue(all(e["who"] == "Sara" for e in events))

    def test_other_teams_activity_is_excluded(self):
        Conversation.objects.create(user=self.outsider, title="not yours")
        self.assertEqual(self.get(self.manager).context["dash"]["activity"], [])

    def test_quick_actions_offer_only_what_a_manager_may_do(self):
        keys = [a["key"] for a in self.get(self.manager).context["dash"]["quick_actions"]]
        self.assertEqual(keys, ["conversation", "project", "projects", "team", "notifications", "billing"])
        self.assertNotIn("invite", keys)

    def test_members_near_their_limit_become_an_attention_item(self):
        UsageLimit.objects.create(user=self.member, monthly_token_cap=100)
        reply(self.member, count=10)  # 10 replies x 10 tokens = 100 of 100
        item = next(i for i in self.get(self.manager).context["dash"]["attention"] if i["key"] == "usage80")
        self.assertIn("1 team member(s) are over 80 % of their limit.", item["text"])

    def test_a_manager_without_a_team_still_gets_a_useful_page(self):
        lone = make("lone.mgr@corp.io", User.Role.MANAGER, department=self.dept_a)
        response = self.get(lone)
        self.assertEqual(response.status_code, 200)
        self.assertIn("noteam", self.attention_keys(response))
        self.assertContains(response, "assigned a team")

    def test_the_existing_team_management_sections_remain(self):
        response = self.get(self.manager)
        self.assertContains(response, "Team model access")
        self.assertContains(response, 'id="team-activity"')


# ---------------------------------------------------------------------------------------------------------------------
class UserDashboardTests(DashboardTestCase):
    def test_welcome_uses_the_first_name_else_the_email_name(self):
        self.assertContains(self.get(self.member), "Good")
        self.assertEqual(self.get(self.member).context["dash"]["greeting"]["name"], "Sara")
        self.assertEqual(self.get(self.user).context["dash"]["greeting"]["name"], "Ahmed Khan")
        self.assertNotContains(self.get(self.user), "ahmed.khan@corp.io</h1>")

    def test_continue_working_is_the_users_own_latest_conversation(self):
        project = Project.objects.create(user=self.user, name="Website Redesign")
        older = Conversation.objects.create(user=self.user, title="Older")
        latest = Conversation.objects.create(user=self.user, title="Homepage design", project=project)
        now = timezone.now()  # explicit times: two rows created back to back can share a timestamp on Windows
        Conversation.objects.filter(pk=older.pk).update(updated_at=now - timedelta(hours=2))
        Conversation.objects.filter(pk=latest.pk).update(updated_at=now - timedelta(minutes=12))
        Conversation.objects.create(user=self.outsider, title="Someone else's newest")
        dash = self.get(self.user).context["dash"]
        self.assertEqual(dash["continue_item"]["title"], "Homepage design")
        self.assertEqual(dash["continue_item"]["subtitle"], "Project: Website Redesign")
        self.assertEqual(
            dash["continue_item"]["url"], reverse("chat:chat_conversation", kwargs={"conversation_id": latest.pk})
        )
        self.assertEqual(
            [c["title"] for c in dash["recent_conversations"]], ["Older"], "the continue item is not repeated"
        )

    def test_no_activity_shows_an_empty_state_not_invented_content(self):
        response = self.get(self.user)
        self.assertIsNone(response.context["dash"]["continue_item"])
        self.assertContains(response, "No conversations yet")
        self.assertContains(response, "No projects yet")
        self.assertContains(response, "Create your first project to organize your work.")
        self.assertContains(response, "all caught up")

    def test_my_projects_are_only_mine_and_bounded(self):
        for n in range(8):
            Project.objects.create(user=self.user, name=f"Mine {n}")
        Project.objects.create(user=self.outsider, name="Not mine")
        projects = self.get(self.user).context["dash"]["projects"]
        self.assertEqual(len(projects), 5)
        self.assertNotIn("Not mine", [p["name"] for p in projects])

    def test_recent_conversations_are_bounded_and_own(self):
        for n in range(9):
            Conversation.objects.create(user=self.user, title=f"C{n}")
        Conversation.objects.create(user=self.outsider, title="Foreign")
        dash = self.get(self.user).context["dash"]
        self.assertLessEqual(len(dash["recent_conversations"]), 4)
        self.assertNotIn("Foreign", [c["title"] for c in dash["recent_conversations"]])

    def test_usage_shows_the_existing_widget_and_warns_near_a_limit(self):
        UsageLimit.objects.create(user=self.user, monthly_token_cap=100)
        reply(self.user, count=9)  # 90 of 100 tokens
        response = self.get(self.user)
        self.assertContains(response, 'data-dash="usage-warning"')
        self.assertContains(response, "approaching your usage limit")
        reply(self.user, count=3)
        self.assertContains(self.get(self.user), "reached a usage limit")

    def test_no_warning_when_usage_is_fine_or_there_is_no_limit(self):
        self.assertNotContains(self.get(self.user), 'data-dash="usage-warning"')
        UsageLimit.objects.create(user=self.user, monthly_token_cap=100000)
        reply(self.user, count=1)
        self.assertNotContains(self.get(self.user), 'data-dash="usage-warning"')

    def test_notifications_put_action_required_first(self):
        notify_admin(self.user, NotificationType.ACCOUNT_CREATED, "Welcome")
        notify_admin(self.user, NotificationType.TRIAL_EXPIRED, "Trial ended")
        notes = self.get(self.user).context["dash"]["notifications"]
        self.assertEqual(notes["unread"], 2)
        self.assertEqual(notes["action_required"], 1)
        self.assertEqual([n["priority"] for n in notes["items"]], ["action", "info"])

    def test_quick_actions_and_shortcuts_reuse_the_chat_starters(self):
        dash = self.get(self.user).context["dash"]
        self.assertEqual(
            [a["key"] for a in dash["quick_actions"]],
            ["new_conversation", "new_project", "upload_document", "compare_models"],
        )
        upload = next(a for a in dash["quick_actions"] if a["key"] == "upload_document")
        self.assertEqual(upload["url"], reverse("chat:create_conversation"))
        self.assertEqual(upload["post"]["start"], "summarize")
        self.assertEqual([s["key"] for s in dash["shortcuts"]], ["draft", "code"])
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.USER, feature_key="projects", defaults={"is_enabled": False}
        )
        dash = self.get(self.user).context["dash"]
        self.assertNotIn("new_project", [a["key"] for a in dash["quick_actions"]])
        self.assertIsNone(dash["projects"])

    def test_a_quick_action_really_starts_a_conversation(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("chat:create_conversation"), {"start": "summarize", "starter_text": "Summarize"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Conversation.objects.filter(user=self.user).count(), 1)

    def test_nothing_about_infrastructure_or_other_peoples_data(self):
        Provider.objects.create(
            name="Acme",
            slug="acme",
            adapter_type="openai_compatible",
            is_connected=True,
            last_sync_status="failed",
            last_sync_error="boom",
        )
        Conversation.objects.create(user=self.outsider, title="Bilal private chat")
        response = self.get(self.user)
        for word in INFRA_WORDS + ("Acme", "boom", "Bilal private chat", "Audit"):
            self.assertNotContains(response, word)

    def test_existing_plan_card_and_usage_widget_are_kept(self):
        response = self.get(self.user)
        self.assertContains(response, "Your plan")
        self.assertContains(response, "Your usage")


# ---------------------------------------------------------------------------------------------------------------------
class PermissionBoundaryTests(DashboardTestCase):
    def test_anonymous_visitors_are_sent_to_login(self):
        for name in ("accounts:dashboard", "governance:dashboard", "governance:manager_dashboard"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn(reverse("accounts:login"), response.url)

    def test_the_admin_dashboard_url_stays_forbidden_to_users_and_managers(self):
        for who in (self.user, self.manager):
            self.assertEqual(self.get(who, "governance:dashboard").status_code, 403)

    def test_a_user_asking_for_the_manager_page_is_refused(self):
        self.assertEqual(self.get(self.user, "governance:manager_dashboard").status_code, 403)

    def test_each_role_gets_exactly_its_own_dashboard_at_the_home_address(self):
        expected = {
            self.superadmin: "superadmin",
            self.admin: "admin",
            self.manager: "manager",
            self.user: "user",
        }
        for who, marker in expected.items():
            body = self.get(who).content.decode()
            self.assertIn(f'data-dashboard="{marker}"', body)
            for other in {"superadmin", "admin", "manager", "user"} - {marker}:
                self.assertNotIn(f'data-dashboard="{other}"', body)

    def test_the_superadmin_only_detail_never_reaches_an_admin_even_by_link_guessing(self):
        response = self.get(self.admin)
        body = response.content.decode()
        for name in ("governance:maintenance", "governance:brand_theme", "governance:media"):
            self.assertNotIn(reverse(name), body.split("</aside>")[-1])
        self.client.force_login(self.admin)
        for name in ("governance:maintenance", "governance:brand_theme", "governance:media"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_links_are_conveniences_only_the_target_page_still_authorizes(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("billing:invoices")).status_code, 403)
        self.assertEqual(self.client.get(reverse("governance:audit_logs")).status_code, 403)


# ---------------------------------------------------------------------------------------------------------------------
class EfficiencyTests(DashboardTestCase):
    def queries(self, who):
        """Queries the dashboard's own builder runs (the surrounding pages have existing per-person helpers of their
        own - the org usage ring, the manager's member rows - which are not part of what was added)."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = who
        with CaptureQueriesContext(connection) as captured:
            if who.role == User.Role.USER:
                dashboards.user_dashboard(who)
            elif who.role == User.Role.MANAGER:
                dashboards.manager_dashboard(request, self.team, User.objects.filter(team=self.team))
            elif who.role == User.Role.ADMIN:
                dashboards.admin_dashboard(request, {}, 0)
            else:
                dashboards.superadmin_dashboard(request, build_system_status(), {}, 0)
        return len(captured)

    def grow(self, n):
        start = getattr(self, "_grown", 0)
        self._grown = start + n
        for i in range(start, start + n):
            person = make(f"extra{i}@corp.io", department=self.dept_a, team=self.team)
            reply(person, count=2)
            Project.objects.create(user=person, name=f"P{i}")
            invoice(person, self.dept_a)
            UsageLimit.objects.create(user=person, monthly_token_cap=1000)
            Notification.objects.create(
                user=self.user, notification_type=NotificationType.ACCOUNT_CREATED, title=f"n{i}"
            )

    def test_query_counts_do_not_grow_with_the_amount_of_data(self):
        self.grow(2)
        people = (self.user, self.manager, self.admin, self.superadmin)
        before = {who.role: self.queries(who) for who in people}
        self.grow(14)
        after = {who.role: self.queries(who) for who in people}
        for role in before:
            self.assertLessEqual(after[role], before[role], f"{role}: {before[role]} -> {after[role]}")

    def test_bounded_lists(self):
        for i in range(30):
            Conversation.objects.create(user=self.user, title=f"c{i}")
            Project.objects.create(user=self.user, name=f"p{i}")
            AuditLog.objects.create(
                actor=self.superadmin, action_type="user.create", target_type="User", target_id=str(i)
            )
            Notification.objects.create(
                user=self.superadmin, notification_type=NotificationType.ACCOUNT_CREATED, title=str(i)
            )
        user_dash = self.get(self.user).context["dash"]
        self.assertLessEqual(len(user_dash["projects"]), 5)
        self.assertLessEqual(len(user_dash["recent_conversations"]), 4)
        super_dash = self.get(self.superadmin).context["dash"]
        self.assertLessEqual(len(super_dash["activity"]), 8)


# ---------------------------------------------------------------------------------------------------------------------
class RealViewEfficiencyTests(DashboardTestCase):
    """The EfficiencyTests above deliberately call dashboards.*_dashboard() directly, bypassing
    DashboardView/ManagerDashboardView - real, pre-existing N+1s in those two views
    (_org_usage_overview's per-user UsageLimit/Message queries, ManagerDashboardView's per-member
    aggregate+engagement_score loop) were invisible to that test by construction. These hit the
    real accounts:dashboard URL instead, so a regression here would actually be caught."""

    def _query_count(self, who):
        with CaptureQueriesContext(connection) as captured:
            self.get(who)
        return len(captured)

    def _grow(self, n, department, team=None):
        start = getattr(self, "_grown", 0)
        self._grown = start + n
        for i in range(start, start + n):
            person = make(f"grow{i}@corp.io", department=department, team=team)
            reply(person, count=2)
            UsageLimit.objects.create(user=person, monthly_token_cap=1000)

    def test_admin_dashboard_query_count_does_not_grow_with_department_size(self):
        self._grow(2, self.dept_a)
        before = self._query_count(self.admin)
        self._grow(15, self.dept_a)
        after = self._query_count(self.admin)
        self.assertLessEqual(after, before, f"admin dashboard: {before} -> {after} queries")

    def test_superadmin_dashboard_query_count_does_not_grow_with_org_size(self):
        self._grow(2, self.dept_a)
        before = self._query_count(self.superadmin)
        self._grow(15, self.dept_b)
        after = self._query_count(self.superadmin)
        self.assertLessEqual(after, before, f"superadmin dashboard: {before} -> {after} queries")

    def test_manager_dashboard_query_count_does_not_grow_with_team_size(self):
        self._grow(2, self.dept_a, team=self.team)
        before = self._query_count(self.manager)
        self._grow(15, self.dept_a, team=self.team)
        after = self._query_count(self.manager)
        self.assertLessEqual(after, before, f"manager dashboard: {before} -> {after} queries")
