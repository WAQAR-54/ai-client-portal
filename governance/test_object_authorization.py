"""Object-level authorization: changing an ID in the URL, the POST body or a query string must never
reach another person's data or another department's objects.

The route x role matrix (test_authorization_matrix) proves who may reach a ROUTE. This proves who
may reach a specific OBJECT behind it, by handing every actor the identifiers of objects they do
not own and asserting BOTH a refusal and that nothing changed."""

import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Team, User
from billing.models import Invoice
from chat.models import Conversation, Message, Project, PromptTemplate
from governance.models import AuditLog, Plan, UsageLimit
from notifications.models import Notification

DENIED = (302, 403, 404)  # 302 = bounced to login; 403/404 = refused
VALIDATES_BEFORE_OWNERSHIP = ("chat:post_arena_message", "chat:generate_media")


class ObjectFixtures(TestCase):
    """alice (user, dept X) owns everything. bob is another user; admin_y another department's Admin."""

    def setUp(self):
        cache.clear()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)

        make = User.objects.create_user
        self.dept_x = Department.objects.create(name="Dept X")
        self.dept_y = Department.objects.create(name="Dept Y")
        self.team_x = Team.objects.create(name="Team X", department=self.dept_x)
        self.team_y = Team.objects.create(name="Team Y", department=self.dept_y)
        self.alice = make(email="alice@example.com", password="pw12345!", department=self.dept_x, team=self.team_x)
        self.bob = make(email="bob@example.com", password="pw12345!", department=self.dept_y, team=self.team_y)
        self.admin_x = make(
            email="admin-x@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.dept_x
        )
        self.admin_y = make(
            email="admin-y@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.dept_y
        )
        self.manager_y = make(
            email="manager-y@example.com", password="pw12345!", role=User.Role.MANAGER, department=self.dept_y
        )
        self.root = make(email="root@example.com", password="pw12345!", role=User.Role.SUPERADMIN)

        self.conversation = Conversation.objects.create(user=self.alice, title="Alice private plan")
        self.user_message = Message.objects.create(
            conversation=self.conversation, role=Message.Role.USER, content="alice secret question"
        )
        self.user_message.attachment.save("alice-contract.pdf", ContentFile(b"%PDF-1.4 alice"), save=False)
        self.user_message.attachment_original_name = "alice-contract.pdf"
        self.user_message.save()
        self.reply = Message.objects.create(
            conversation=self.conversation, role=Message.Role.ASSISTANT, content="alice secret answer"
        )
        self.pending = Message.objects.create(conversation=self.conversation, role=Message.Role.ASSISTANT, content="")
        self.project = Project.objects.create(user=self.alice, name="Alice project")
        self.template = PromptTemplate.objects.create(owner=self.alice, name="Alice tpl", content="text")
        self.notification = Notification.objects.create(user=self.alice, notification_type="usage_warning", title="t")

        self.invoice = Invoice.objects.create(
            department=self.dept_x,
            recipient_user=self.alice,
            plan=Plan.objects.get(name="Premium"),
            issue_date=timezone.localdate(),
            due_date=timezone.localdate() + timedelta(days=14),
            currency="USD",
            subtotal=Decimal("50"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("50"),
            status=Invoice.Status.PENDING_VERIFICATION,
        )
        self.invoice.submitted_proof_image.save("proof.png", ContentFile(b"\x89PNG\r\n\x1a\n0000"), save=True)

    def call(self, actor, method, name, **kwargs):
        self.client.logout()
        if actor is not None:
            self.client.force_login(actor)
        data = kwargs.pop("data", None)
        url = reverse(name, kwargs=kwargs)
        if method == "GET":
            return self.client.get(url, data or {})
        return self.client.post(url, data or {})

    def assertRefused(self, actor, method, name, **kwargs):
        response = self.call(actor, method, name, **kwargs)
        allowed = DENIED + ((400, 429) if name in VALIDATES_BEFORE_OWNERSHIP else ())
        # These two views check the request's own fields (empty prompt, model ids) first and enforce
        # ownership before their first write, so a 400 there still creates nothing; the "changed nothing"
        # test below proves that.
        self.assertIn(response.status_code, allowed, f"{actor} {method} {name} {kwargs} -> {response.status_code}")


class ChatObjectTests(ObjectFixtures):
    def routes(self):
        c, m = self.conversation.pk, self.user_message.pk
        return [
            ("GET", "chat:chat_conversation", {"conversation_id": c}),
            ("POST", "chat:toggle_pin", {"conversation_id": c}),
            ("POST", "chat:delete_conversation", {"conversation_id": c}),
            ("POST", "chat:post_message", {"conversation_id": c, "data": {"content": "injected"}}),
            ("POST", "chat:post_arena_message", {"conversation_id": c, "data": {"content": "x"}}),
            ("POST", "chat:generate_media", {"conversation_id": c, "data": {"prompt": "x"}}),
            (
                "POST",
                "chat:move_conversation_to_project",
                {"conversation_id": c, "data": {"project_id": self.project.pk}},
            ),
            ("GET", "chat:export_conversation_markdown", {"conversation_id": c}),
            ("GET", "chat:export_conversation_text", {"conversation_id": c}),
            ("GET", "chat:export_conversation_pdf", {"conversation_id": c}),
            ("GET", "chat:download_attachment", {"conversation_id": c, "message_id": m}),
            ("GET", "chat:render_message", {"conversation_id": c, "message_id": self.reply.pk}),
            ("GET", "chat:artifact_panel", {"conversation_id": c, "message_id": self.reply.pk}),
            ("POST", "chat:edit_message", {"conversation_id": c, "message_id": m, "data": {"content": "tampered"}}),
            ("POST", "chat:regenerate_message", {"conversation_id": c, "message_id": self.reply.pk}),
            (
                "POST",
                "chat:submit_feedback",
                {"conversation_id": c, "message_id": self.reply.pk, "data": {"rating": "up"}},
            ),
            (
                "GET",
                "chat:stream_message",
                {"conversation_id": c, "message_id": self.pending.pk, "token": self.pending.stream_token},
            ),
            (
                "GET",
                "chat:export_message_document",
                {"conversation_id": c, "message_id": self.reply.pk, "doc_format": "docx"},
            ),
        ]

    def test_nobody_but_the_owner_reaches_a_conversation_by_id(self):
        # Even a SuperAdmin: the chat routes are owner-only by design (privacy of conversations).
        for actor in (None, self.bob, self.admin_x, self.admin_y, self.manager_y, self.root):
            for method, name, kwargs in self.routes():
                self.assertRefused(actor, method, name, **{k: v for k, v in kwargs.items()})

    def test_the_refused_requests_changed_nothing(self):
        for actor in (self.bob, self.admin_y, self.root):
            for method, name, kwargs in self.routes():
                self.call(actor, method, name, **kwargs)
        self.conversation.refresh_from_db()
        self.assertFalse(self.conversation.is_deleted)
        self.assertFalse(self.conversation.is_pinned)
        self.assertIsNone(self.conversation.project_id)
        self.user_message.refresh_from_db()
        self.assertEqual(self.user_message.content, "alice secret question")
        self.assertEqual(Message.objects.filter(conversation=self.conversation).count(), 3)

    def test_the_owner_still_reaches_everything(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse("chat:chat_conversation", kwargs={"conversation_id": self.conversation.pk}))
        self.assertEqual(response.status_code, 200)
        response = self.client.get(
            reverse(
                "chat:download_attachment",
                kwargs={"conversation_id": self.conversation.pk, "message_id": self.user_message.pk},
            )
        )
        self.assertEqual(response.status_code, 200)

    def test_own_conversation_with_someone_elses_message_id_is_refused(self):
        """The identifier pair must agree: bob's own conversation + alice's message id."""
        bob_conversation = Conversation.objects.create(user=self.bob)
        self.client.force_login(self.bob)
        for name in ("chat:download_attachment", "chat:render_message", "chat:artifact_panel"):
            response = self.client.get(
                reverse(name, kwargs={"conversation_id": bob_conversation.pk, "message_id": self.user_message.pk})
            )
            self.assertEqual(response.status_code, 404, name)
        for name in ("chat:edit_message", "chat:regenerate_message", "chat:submit_feedback"):
            response = self.client.post(
                reverse(name, kwargs={"conversation_id": bob_conversation.pk, "message_id": self.user_message.pk}),
                {"content": "x", "rating": "up"},
            )
            self.assertIn(response.status_code, (403, 404), name)
        self.user_message.refresh_from_db()
        self.assertEqual(self.user_message.content, "alice secret question")

    def test_a_posted_project_id_of_another_user_is_never_attached(self):
        bob_conversation = Conversation.objects.create(user=self.bob)
        self.client.force_login(self.bob)
        self.client.post(
            reverse("chat:move_conversation_to_project", kwargs={"conversation_id": bob_conversation.pk}),
            {"project_id": self.project.pk},
        )
        bob_conversation.refresh_from_db()
        self.assertNotEqual(bob_conversation.project_id, self.project.pk)
        self.client.post(reverse("chat:create_conversation"), {"project_id": self.project.pk})
        self.assertFalse(Conversation.objects.filter(user=self.bob, project=self.project).exists())

    def test_projects_and_templates_are_personal(self):
        self.client.force_login(self.bob)
        for name, kwargs in (
            ("chat:rename_project", {"project_id": self.project.pk}),
            ("chat:delete_project", {"project_id": self.project.pk}),
            ("chat:delete_prompt_template", {"template_id": self.template.pk}),
        ):
            response = self.client.post(reverse(name, kwargs=kwargs), {"name": "hijacked"})
            self.assertIn(response.status_code, (403, 404), name)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Alice project")
        self.assertTrue(PromptTemplate.objects.filter(pk=self.template.pk).exists())

    def test_a_conversation_listing_only_shows_the_actors_own(self):
        self.client.force_login(self.bob)
        for name in ("chat:search_conversations", "chat:load_more_conversations", "chat:chat_home"):
            response = self.client.get(reverse(name), {"q": "Alice", "offset": 0})
            self.assertNotContains(response, "Alice private plan", msg_prefix=name)

    def test_notifications_belong_to_their_recipient(self):
        self.client.force_login(self.bob)
        response = self.client.post(
            reverse("notifications:mark_read", kwargs={"notification_id": self.notification.pk})
        )
        self.assertIn(response.status_code, (403, 404))
        self.notification.refresh_from_db()
        self.assertFalse(self.notification.is_read)


class BillingObjectTests(ObjectFixtures):
    def invoice_routes(self):
        i = self.invoice.pk
        return [
            ("GET", "billing:invoice_detail", {"invoice_id": i}),
            ("GET", "billing:download_invoice_pdf", {"invoice_id": i}),
            ("GET", "billing:invoice_proof", {"invoice_id": i}),
            ("POST", "billing:toggle_invoice_status", {"invoice_id": i}),
            ("POST", "billing:verify_invoice_payment", {"invoice_id": i}),
            ("POST", "billing:reject_invoice_payment", {"invoice_id": i, "data": {"reason": "no"}}),
            ("POST", "billing:email_invoice", {"invoice_id": i}),
            ("POST", "billing:delete_invoice", {"invoice_id": i}),
            ("POST", "billing:submit_payment_proof", {"invoice_id": i}),
            ("POST", "billing:request_refund", {"invoice_id": i, "data": {"reason": "x"}}),
        ]

    def test_another_user_and_another_departments_admin_reach_none_of_it(self):
        for actor in (None, self.bob, self.manager_y, self.admin_y):
            for method, name, kwargs in self.invoice_routes():
                self.assertRefused(actor, method, name, **kwargs)

    def test_nothing_about_the_invoice_changed(self):
        for actor in (self.bob, self.manager_y, self.admin_y):
            for method, name, kwargs in self.invoice_routes():
                self.call(actor, method, name, **kwargs)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PENDING_VERIFICATION)
        self.assertTrue(Invoice.objects.filter(pk=self.invoice.pk).exists())

    def test_the_recipient_sees_their_invoice_but_cannot_run_admin_actions_on_it(self):
        self.assertEqual(
            self.call(self.alice, "GET", "billing:invoice_detail", invoice_id=self.invoice.pk).status_code, 200
        )
        self.assertEqual(
            self.call(self.alice, "GET", "billing:invoice_proof", invoice_id=self.invoice.pk).status_code, 200
        )
        for name in ("billing:verify_invoice_payment", "billing:reject_invoice_payment", "billing:delete_invoice"):
            self.assertRefused(self.alice, "POST", name, invoice_id=self.invoice.pk)

    def test_the_departments_own_admin_and_a_superadmin_can(self):
        for actor in (self.admin_x, self.root):
            self.assertEqual(
                self.call(actor, "GET", "billing:invoice_detail", invoice_id=self.invoice.pk).status_code, 200
            )
            self.assertEqual(
                self.call(actor, "GET", "billing:invoice_proof", invoice_id=self.invoice.pk).status_code, 200
            )

    def test_a_departmentless_admin_does_not_match_a_departmentless_invoice(self):
        floating_admin = User.objects.create_user(
            email="floating-admin@example.com", password="pw12345!", role=User.Role.ADMIN, department=None
        )
        Invoice.objects.filter(pk=self.invoice.pk).update(department=None)
        self.assertRefused(floating_admin, "GET", "billing:invoice_detail", invoice_id=self.invoice.pk)
        self.assertRefused(floating_admin, "GET", "billing:invoice_proof", invoice_id=self.invoice.pk)

    def test_another_departments_admin_cannot_invoice_a_team_that_is_not_theirs(self):
        before = Invoice.objects.count()
        self.assertRefused(self.admin_y, "POST", "billing:generate_team_invoice", team_id=self.team_x.pk)
        self.assertEqual(Invoice.objects.count(), before)

    def test_share_links_need_the_real_token(self):
        for name in ("billing:public_invoice", "billing:public_invoice_pdf"):
            response = self.client.get(reverse(name, kwargs={"token": "not-a-real-token"}))
            self.assertEqual(response.status_code, 404, name)


class GovernanceObjectTests(ObjectFixtures):
    """A department's Admin manages their OWN department's Users and Managers - nobody above them,
    nobody beside them, nothing in another department."""

    USER_ACTIONS = (
        ("GET", "governance:user_edit_form", None),
        ("POST", "governance:toggle_user_active", None),
        ("POST", "governance:change_user_email", {"email": "hijack@example.com"}),
        (
            "POST",
            "governance:reset_user_password",
            {"new_password1": "Sup3r-Secret-Pass!", "new_password2": "Sup3r-Secret-Pass!"},
        ),
        ("POST", "governance:change_user_role", {"role": "manager", "team_id": "1", "confirmed": "1"}),
        ("POST", "governance:change_user_department", {"department_id": "1"}),
        ("POST", "governance:delete_user", None),
    )

    def act(self, actor, target, action):
        method, name, data = action
        return self.call(actor, method, name, user_id=target.pk, data=data)

    def assertAllRefused(self, actor, target):
        before = (target.email, target.is_active, target.role, target.department_id, target.password)
        for action in self.USER_ACTIONS:
            response = self.act(actor, target, action)
            self.assertIn(
                response.status_code,
                (403, 404),
                f"{actor.role} -> {action[1]} on {target.role}: {response.status_code}",
            )
        target.refresh_from_db()
        self.assertEqual((target.email, target.is_active, target.role, target.department_id, target.password), before)
        self.assertTrue(User.objects.filter(pk=target.pk).exists())

    def test_an_admin_cannot_touch_another_departments_user(self):
        self.assertAllRefused(self.admin_x, self.bob)

    def test_an_admin_cannot_touch_a_superadmin_even_in_their_own_department(self):
        boss = User.objects.create_user(
            email="boss@example.com", password="pw12345!", role=User.Role.SUPERADMIN, department=self.dept_x
        )
        self.assertAllRefused(self.admin_x, boss)

    def test_an_admin_cannot_touch_a_peer_admin(self):
        peer = User.objects.create_user(
            email="peer-admin@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.dept_x
        )
        self.assertAllRefused(self.admin_x, peer)

    def test_an_admin_without_a_department_cannot_reach_departmentless_accounts(self):
        floating_admin = User.objects.create_user(
            email="floating@example.com", password="pw12345!", role=User.Role.ADMIN, department=None
        )
        self.assertEqual(self.root.department_id, None)
        self.assertAllRefused(floating_admin, self.root)
        floating_user = User.objects.create_user(
            email="floating-user@example.com", password="pw12345!", department=None
        )
        self.assertAllRefused(floating_admin, floating_user)

    def test_an_admin_still_manages_their_own_departments_users(self):
        self.assertEqual(
            self.call(self.admin_x, "GET", "governance:user_edit_form", user_id=self.alice.pk).status_code, 200
        )
        response = self.call(self.admin_x, "POST", "governance:toggle_user_active", user_id=self.alice.pk)
        self.assertEqual(response.status_code, 302)
        self.alice.refresh_from_db()
        self.assertFalse(self.alice.is_active)

    def test_a_superadmin_manages_anyone(self):
        self.assertEqual(self.call(self.root, "GET", "governance:user_edit_form", user_id=self.bob.pk).status_code, 200)

    def test_a_manager_and_a_user_reach_none_of_the_user_actions(self):
        for actor in (self.manager_y, self.bob, self.alice):
            for action in self.USER_ACTIONS:
                self.assertRefused(actor, action[0], action[1], user_id=self.alice.pk, data=action[2])

    def test_usage_limits_are_scoped_to_the_admins_department(self):
        other = UsageLimit.objects.create(user=self.bob, daily_token_cap=10)
        own = UsageLimit.objects.create(user=self.alice, daily_token_cap=10)
        self.assertRefused(self.admin_x, "POST", "governance:limit_delete", limit_id=other.pk)
        self.assertRefused(self.admin_x, "GET", "governance:limit_edit", limit_id=other.pk)
        self.assertTrue(UsageLimit.objects.filter(pk=other.pk).exists())
        self.assertEqual(self.call(self.admin_x, "POST", "governance:limit_delete", limit_id=own.pk).status_code, 302)
        self.assertFalse(UsageLimit.objects.filter(pk=own.pk).exists())

    def test_user_overrides_are_scoped(self):
        self.assertRefused(self.admin_x, "GET", "governance:user_overrides", user_id=self.bob.pk)
        self.assertRefused(self.admin_x, "POST", "governance:clear_user_overrides", user_id=self.bob.pk)

    def test_teams_and_departments_of_another_department_are_untouchable(self):
        self.assertRefused(self.admin_x, "POST", "governance:delete_team", team_id=self.team_y.pk)
        self.assertTrue(Team.objects.filter(pk=self.team_y.pk).exists())
        for name in ("governance:update_department", "governance:delete_department", "governance:system_prompt"):
            self.assertRefused(self.admin_x, "POST", name, department_id=self.dept_y.pk, data={"name": "x"})
        self.assertTrue(Department.objects.filter(pk=self.dept_y.pk).exists())

    def test_platform_configuration_is_superadmin_only_by_object_id(self):
        from providers.models import Provider, ProviderModel

        provider = Provider.objects.get(slug="openai")
        model = ProviderModel.objects.create(provider=provider, model_id="obj-authz-model")
        plan = Plan.objects.get(name="Premium")
        attempts = [
            ("POST", "providers:connect", {"provider_id": provider.pk, "data": {"api_key": "x"}}),
            ("POST", "providers:disconnect", {"provider_id": provider.pk}),
            ("POST", "providers:resync", {"provider_id": provider.pk}),
            ("POST", "providers:toggle_model", {"model_id": model.pk}),
            ("POST", "governance:toggle_model_enabled", {"model_id": model.pk}),
            ("POST", "governance:delete_model", {"model_id": model.pk}),
            ("POST", "governance:update_model_pricing", {"model_id": model.pk, "data": {"input_price": "1"}}),
            ("GET", "governance:plan_edit", {"plan_id": plan.pk}),
            ("GET", "governance:plan_manage", {"plan_id": plan.pk}),
            ("POST", "governance:update_plan_access", {"plan_id": plan.pk}),
        ]
        enabled_before = ProviderModel.objects.get(pk=model.pk).is_enabled
        for actor in (None, self.alice, self.manager_y, self.admin_x, self.admin_y):
            for method, name, kwargs in attempts:
                self.assertRefused(actor, method, name, **kwargs)
        self.assertEqual(ProviderModel.objects.get(pk=model.pk).is_enabled, enabled_before)
        self.assertTrue(ProviderModel.objects.filter(pk=model.pk).exists())

    def test_media_items_are_superadmin_only_by_object_id(self):
        for actor in (None, self.alice, self.manager_y, self.admin_x):
            for name in ("governance:media_download", "governance:media_preview"):
                self.assertRefused(actor, "GET", name, source="chat", pk=self.user_message.pk)
            self.assertRefused(actor, "GET", "governance:media_download", source="proof", pk=self.invoice.pk)

    def test_the_audit_log_only_shows_the_admins_own_departments_events(self):
        AuditLog.objects.create(
            actor=self.admin_y, action_type="user.suspend", target_type="User", target_id=str(self.bob.pk)
        )
        AuditLog.objects.create(
            actor=self.admin_x, action_type="user.activate", target_type="User", target_id=str(self.alice.pk)
        )
        self.client.force_login(self.admin_x)
        response = self.client.get(reverse("governance:audit_logs"))
        self.assertContains(response, "user.activate")
        rows = [log.action_type for log in response.context["logs"]]
        self.assertIn("user.activate", rows)
        self.assertNotIn("user.suspend", rows)
        self.assertNotContains(response, "admin-y@example.com")
        self.assertNotIn("user.suspend", list(response.context["action_types"]), "filter list leaks other departments")


class DepartmentlessAdminTests(ObjectFixtures):
    """An Admin with no department has no department to administer. filter(department_id=None) and
    None == None both match every department-less row - which is where SuperAdmins usually live."""

    def setUp(self):
        super().setUp()
        self.floating = User.objects.create_user(
            email="floating@example.com", password="pw12345!", role=User.Role.ADMIN, department=None
        )
        self.orphan_user = User.objects.create_user(email="orphan@example.com", password="pw12345!", department=None)
        self.orphan_invoice = Invoice.objects.create(
            department=None,
            recipient_user=self.orphan_user,
            plan=Plan.objects.get(name="Premium"),
            issue_date=timezone.localdate(),
            due_date=timezone.localdate() + timedelta(days=14),
            currency="USD",
            subtotal=Decimal("10"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("10"),
        )

    def test_the_users_list_and_invoice_list_are_empty(self):
        self.client.force_login(self.floating)
        users = self.client.get(reverse("governance:users"))
        self.assertNotContains(users, "root@example.com")
        self.assertNotContains(users, "orphan@example.com")
        invoices = self.client.get(reverse("billing:invoices"))
        self.assertNotContains(invoices, self.orphan_invoice.invoice_number)

    def test_upgrade_requests_of_departmentless_users_cannot_be_resolved(self):
        from governance.models import UpgradeRequest

        request = UpgradeRequest.objects.create(user=self.orphan_user)
        self.assertRefused(
            self.floating,
            "POST",
            "governance:resolve_upgrade_request",
            request_id=request.pk,
            data={"action": "dismiss"},
        )
        request.refresh_from_db()
        self.assertEqual(request.status, UpgradeRequest.Status.PENDING)

    def test_a_departmentless_admin_cannot_set_a_limit_on_a_departmentless_user(self):
        before = UsageLimit.objects.count()
        response = self.call(
            self.floating, "POST", "governance:limit_new", data={"user_id": self.orphan_user.pk, "daily_token_cap": "5"}
        )
        self.assertIn(response.status_code, (302, 400, 403, 404))
        self.assertEqual(UsageLimit.objects.count(), before)
