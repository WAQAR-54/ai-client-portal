"""Authorization matrix: what each role is actually let into, tested at the backend
endpoint - never inferred from which buttons a template shows.

  1. ROUTE GATES  - every URL in the project is requested as each role (accounts/authz_matrix.py)
                    and checked against invariants and a reviewed golden file.
  2. OBJECT SCOPE - things a route gate cannot express: another user's notification, another
                    team's member.

The chat, governance and billing object-level rules already have extensive tests in their own
modules (cross-user conversation/message/project access, department-scoped user management,
department-scoped invoices); this module covers the routes and objects those do not.

`AUTHZ_UPDATE=1 python manage.py test governance.test_authorization_matrix` rewrites the golden
file after a deliberate permission change - review the diff. `AUTHZ_DUMP=<file>` writes the full
route x role table as JSON.
"""

import json
import os
from collections import defaultdict
from pathlib import Path

from django.test import Client, TestCase
from django.urls import reverse

from accounts.authz_matrix import is_public, iter_routes, probe
from accounts.models import Department, Team, User
from notifications.models import Notification

GOLDEN = Path(__file__).with_name("authz_expected.json")
ROLES = ("anonymous", "user", "member", "manager", "dept_admin", "admin", "superadmin")
HIERARCHY = ("anonymous", "user", "manager", "admin", "superadmin")
REFUSED = ("DENY", "LOGIN")

# Routes an anonymous visitor may reach on purpose.
PUBLIC_EXACT = {"/", "/docs/", "/docs/guides/", "/admin/login/", "/billing/pricing/"}
PUBLIC_PREFIXES = ("/billing/share/", "/notifications/track/")
# Manager-only on purpose: a Manager runs a team, and Admin/SuperAdmin manage teams through the
# governance pages instead - so these are the only routes where a higher role is refused.
MANAGER_ONLY = {
    "/governance/my-team/members/999999/permissions/",
    "/governance/my-team/members/999999/permissions/999999/toggle/",
    "/governance/my-team/members/999999/remove/",
    "/governance/my-team/models/999999/toggle/",
}


def _make_users(department):
    make = User.objects.create_user
    users = {
        "user": make(email="plain@example.com", password="pw12345!"),
        "manager": make(email="manager@example.com", password="pw12345!", role=User.Role.MANAGER),
        "dept_admin": make(
            email="deptadmin@example.com", password="pw12345!", role=User.Role.ADMIN, department=department
        ),
        "admin": make(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN),
        "superadmin": make(email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True),
    }
    team = Team.objects.create(name="Team A", department=department, manager=users["manager"])
    users["member"] = make(email="member@example.com", password="pw12345!", team=team)
    return users, team


def build_matrix(users):
    """{(name, path): {role: (method, verdict, status, location)}} for every non-regex route."""
    matrix = defaultdict(dict)
    routes = sorted(set(iter_routes()), key=lambda item: item[1])
    for role in ROLES:
        client = Client()
        if role != "anonymous":
            client.force_login(users[role])
        for name, path in routes:
            if "logout" in path or "sign-out-all" in path:
                continue  # ends the very session being probed (accounts, Django admin, "sign out all sessions")
            matrix[(name, path)][role] = probe(client, path)
    return matrix


def passes(verdicts, role):
    """True when the role got PAST the permission check (a 404 for the placeholder id counts:
    the view ran its lookup, so the gate let the role in)."""
    return verdicts[role][1] not in REFUSED


def minimum_role(verdicts):
    return next((role for role in HIERARCHY if passes(verdicts, role)), "none")


class RouteGateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.users, cls.team = _make_users(Department.objects.create(name="Dept A"))
        cls.matrix = build_matrix(cls.users)  # ~40s: built once for every test below

    def test_dump_the_matrix_for_review(self):
        target = os.environ.get("AUTHZ_DUMP")
        if not target:
            self.skipTest("set AUTHZ_DUMP=<file> to write the route x role table")
        rows = [
            {"name": name, "path": path, **{role: f"{v[1]}/{v[2]}" for role, v in verdicts.items()}}
            for (name, path), verdicts in self.matrix.items()
        ]
        Path(target).write_text(json.dumps(rows, indent=1), encoding="utf-8")

    def test_no_route_crashes_for_any_role(self):
        crashes = [
            (role, path, v[2])
            for (_name, path), verdicts in self.matrix.items()
            for role, v in verdicts.items()
            if v[1] == "ERROR"
        ]
        self.assertEqual(crashes, [])

    def test_an_anonymous_visitor_is_refused_everywhere_except_the_public_allowlist(self):
        leaks = []
        for (_name, path), verdicts in self.matrix.items():
            if is_public(path) or path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES):
                continue
            if passes(verdicts, "anonymous"):
                leaks.append((path, verdicts["anonymous"][1], verdicts["anonymous"][2]))
        self.assertEqual(leaks, [], "reachable without logging in - add to the allowlist ONLY if intended")

    def test_the_public_token_routes_do_not_leak_for_a_bad_token(self):
        for path in ("/billing/share/x/", "/billing/share/x/pdf/", "/notifications/track/x.gif"):
            verdicts = next(v for (_n, p), v in self.matrix.items() if p == path)
            self.assertEqual(verdicts["anonymous"][2], 404, path)

    def test_member_equals_user_and_department_admin_equals_admin_at_the_gate(self):
        """Team membership and department scoping are enforced INSIDE the views (object level);
        they must not change which routes a role can enter."""
        for (_name, path), verdicts in self.matrix.items():
            self.assertEqual(passes(verdicts, "member"), passes(verdicts, "user"), path)
            self.assertEqual(passes(verdicts, "dept_admin"), passes(verdicts, "admin"), path)

    def test_a_higher_role_is_never_locked_out_of_what_a_lower_role_may_do_except_manager_only_routes(self):
        inversions = set()
        for (_name, path), verdicts in self.matrix.items():
            flags = [passes(verdicts, role) for role in HIERARCHY]
            if any(flags[i] and not flags[j] for i in range(len(flags)) for j in range(i + 1, len(flags))):
                inversions.add(path)
        self.assertEqual(inversions, MANAGER_ONLY)

    def test_the_sensitive_areas_have_the_minimum_role_they_are_meant_to_have(self):
        by_name = {name: minimum_role(verdicts) for (name, _p), verdicts in self.matrix.items() if name}
        expectations = {
            # SuperAdmin only: credentials, models, plans, departments, data handling, org billing
            "superadmin": [
                "list", "connect", "approve", "reject", "resync", "disconnect", "update_region", "toggle_model",
                "models", "delete_model", "update_model_pricing", "toggle_model_enabled", "plan_manage", "plan_new",
                "plan_edit", "update_plan_access", "departments", "add_department", "delete_department",
                "branding", "data_handling", "feature_visibility", "email_logs", "delete_email_logs",
                "toggle_mfa_required", "delete_user", "change_user_department", "regional_pricing",
                "organization_billing", "delete_invoice", "routing_rules", "capability_limits",
                "compliance_routing", "budget_automation",
            ],  # fmt: skip
            # Admin (scoped to their department inside the view): people, billing operations, reports
            "admin": [
                "dashboard", "users", "add_user", "change_user_role", "change_user_email", "change_user_plan",
                "reset_user_password", "toggle_user_active", "audit_logs", "invoices", "generate_invoice",
                "verify_invoice_payment", "reject_invoice_payment", "resolve_refund_request", "refund_requests",
                "usage", "revenue_report", "growth_report", "upgrade_requests", "resolve_upgrade_request",
            ],  # fmt: skip
            "manager": ["manager_dashboard", "manager_member_permissions", "remove_team_member", "toggle_team_model"],
            # Anyone signed in: their own account, invoices, notifications, plan
            "user": [
                "my_invoices", "invoice_detail", "download_invoice_pdf", "invoice_proof", "submit_payment_proof",
                "request_refund", "cancel_plan", "resume_plan", "mark_read", "mark_all_read", "profile",
            ],  # fmt: skip
        }
        wrong = {
            name: (wanted, by_name.get(name, "MISSING"))
            for wanted, names in expectations.items()
            for name in names
            if by_name.get(name) != wanted
        }
        self.assertEqual(wrong, {}, "name: (expected minimum role, actual)")

    def test_the_route_gates_match_the_reviewed_golden_file(self):
        actual = {
            path: minimum_role(v) for (_n, path), v in sorted(self.matrix.items()) if not path.startswith("/admin/")
        }
        if os.environ.get("AUTHZ_UPDATE"):
            GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        changed = {
            path: (expected.get(path, "NEW ROUTE"), actual.get(path, "REMOVED"))
            for path in set(expected) | set(actual)
            if expected.get(path) != actual.get(path)
        }
        self.assertEqual(
            changed,
            {},
            "a route's minimum role changed - if deliberate, rerun with AUTHZ_UPDATE=1 and review authz_expected.json",
        )

    def test_the_django_admin_site_is_closed_to_everyone_but_staff(self):
        for role in ("anonymous", "user", "member", "manager", "dept_admin", "admin"):
            client = Client()
            if role != "anonymous":
                client.force_login(self.users[role])
            response = client.get("/admin/")
            self.assertEqual(response.status_code, 302, role)
            self.assertIn("/admin/login/", response["Location"], role)


class ObjectScopeTests(TestCase):
    """Objects that belong to someone: the route gate lets the role in, the view must still
    refuse an object that is not theirs."""

    @classmethod
    def setUpTestData(cls):
        cls.department = Department.objects.create(name="Dept A")
        cls.other_department = Department.objects.create(name="Dept B")
        cls.users, cls.team = _make_users(cls.department)
        cls.other_manager = User.objects.create_user(
            email="manager-b@example.com", password="pw12345!", role=User.Role.MANAGER
        )
        cls.other_team = Team.objects.create(name="Team B", department=cls.other_department, manager=cls.other_manager)
        cls.other_member = User.objects.create_user(
            email="member-b@example.com", password="pw12345!", team=cls.other_team
        )

    # ---- notifications ------------------------------------------------------------------
    def _notification(self, user):
        return Notification.objects.create(user=user, notification_type="plan_change", title="t", body="b")

    def test_a_user_cannot_mark_someone_elses_notification_read(self):
        theirs = self._notification(self.users["member"])
        self.client.force_login(self.users["user"])
        self.assertEqual(self.client.post(reverse("notifications:mark_read", args=[theirs.id])).status_code, 404)
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    def test_deleting_notifications_only_ever_touches_your_own(self):
        mine, theirs = self._notification(self.users["user"]), self._notification(self.users["member"])
        self.client.force_login(self.users["user"])
        self.client.post(reverse("notifications:delete_notifications"), {"delete_all": "1"})
        self.client.post(reverse("notifications:delete_notifications"), {"notification_ids": [mine.id, theirs.id]})
        self.assertTrue(Notification.objects.filter(id=theirs.id).exists())
        self.assertFalse(Notification.objects.filter(id=mine.id).exists())

    def test_mark_all_read_only_touches_your_own(self):
        theirs = self._notification(self.users["member"])
        self.client.force_login(self.users["user"])
        self.client.post(reverse("notifications:mark_all_read"))
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    def test_the_notification_list_shows_only_your_own(self):
        self._notification(self.users["member"])
        self.client.force_login(self.users["user"])
        self.assertEqual(self.client.get(reverse("notifications:list")).context["page_obj"].paginator.count, 0)

    # ---- team scope ---------------------------------------------------------------------
    def test_a_manager_cannot_touch_a_member_of_another_team(self):
        self.client.force_login(self.users["manager"])
        permissions = self.client.get(reverse("governance:manager_member_permissions", args=[self.other_member.id]))
        removal = self.client.post(reverse("governance:remove_team_member", args=[self.other_member.id]))
        self.assertIn(permissions.status_code, (403, 404))
        self.assertIn(removal.status_code, (403, 404))
        self.other_member.refresh_from_db()
        self.assertEqual(self.other_member.team_id, self.other_team.id)  # still in their own team

    def test_a_manager_can_manage_a_member_of_their_own_team(self):
        self.client.force_login(self.users["manager"])
        response = self.client.get(reverse("governance:manager_member_permissions", args=[self.users["member"].id]))
        self.assertEqual(response.status_code, 200)

    def test_a_plain_member_cannot_use_the_managers_pages(self):
        self.client.force_login(self.users["member"])
        for name, args in (
            ("governance:manager_dashboard", []),
            ("governance:manager_member_permissions", [self.users["member"].id]),
        ):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)
