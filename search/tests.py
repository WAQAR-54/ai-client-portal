"""Global Search / Command Palette tests. The load-bearing ones here are authorization: every
category in search/views.py::global_search reuses an existing scoped queryset/helper verbatim
(chat's own conversation filter, governance's _scope_users, billing's scoped_invoices, governance's
media_service.list_items) - these tests exist to prove that composition never leaks a record past
whatever that helper already restricts, not to re-test the helpers themselves (those have their own
test coverage in their own apps)."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, User
from billing.models import Invoice
from chat.models import Conversation, Project
from governance.models import Plan, RoleFeatureToggle


def make_invoice(user, plan, department=None, invoice_number_seed=None):
    due_date = timezone.localdate() + timedelta(days=14)
    invoice = Invoice.objects.create(
        department=department,
        recipient_user=user,
        plan=plan,
        issue_date=due_date - timedelta(days=14),
        due_date=due_date,
        currency="USD",
        subtotal=Decimal("50"),
        tax_rate=Decimal("0"),
        tax_amount=Decimal("0"),
        total=Decimal("50"),
        status=Invoice.Status.UNPAID,
    )
    return invoice


class GlobalSearchAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="user@example.com", password="pw12345!")

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("search:global"), {"q": "hello"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)

    def test_a_role_with_the_feature_turned_off_gets_403(self):
        RoleFeatureToggle.objects.create(role=User.Role.USER, feature_key="quick_switcher", is_enabled=False)
        self.client.login(email="user@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": "hello"})
        self.assertEqual(response.status_code, 403)

    def test_a_role_with_the_feature_on_can_search(self):
        self.client.login(email="user@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": "hello"})
        self.assertEqual(response.status_code, 200)


class ConversationSearchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="user@example.com", password="pw12345!")
        self.other = User.objects.create_user(email="other@example.com", password="pw12345!")
        self.client.login(email="user@example.com", password="pw12345!")

    def test_finds_the_users_own_conversation_by_title(self):
        Conversation.objects.create(user=self.user, title="Quarterly budget planning")
        response = self.client.get(reverse("search:global"), {"q": "budget"})
        self.assertContains(response, "Quarterly budget planning")

    def test_never_returns_another_users_conversation(self):
        Conversation.objects.create(user=self.other, title="Quarterly budget planning")
        response = self.client.get(reverse("search:global"), {"q": "budget"})
        self.assertNotContains(response, "Quarterly budget planning")

    def test_empty_query_shows_recent_conversations(self):
        c = Conversation.objects.create(user=self.user, title="Recent one")
        response = self.client.get(reverse("search:global"), {"q": ""})
        self.assertContains(response, "Recent one")
        self.assertContains(response, reverse("chat:chat_conversation", kwargs={"conversation_id": c.id}))

    def test_empty_query_never_shows_another_users_conversation(self):
        Conversation.objects.create(user=self.other, title="Someone else's chat")
        response = self.client.get(reverse("search:global"), {"q": ""})
        self.assertNotContains(response, "Someone else's chat")

    def test_one_character_query_returns_a_hint_not_results(self):
        Conversation.objects.create(user=self.user, title="a")
        response = self.client.get(reverse("search:global"), {"q": "a"})
        self.assertContains(response, "Type at least 2 characters")

    def test_no_results_state(self):
        response = self.client.get(reverse("search:global"), {"q": "zzz-nothing-matches-zzz"})
        self.assertContains(response, "No results for")


class ProjectSearchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="user@example.com", password="pw12345!")
        self.other = User.objects.create_user(email="other@example.com", password="pw12345!")
        self.client.login(email="user@example.com", password="pw12345!")

    def test_finds_the_users_own_project(self):
        Project.objects.create(user=self.user, name="Website Redesign")
        response = self.client.get(reverse("search:global"), {"q": "redesign"})
        self.assertContains(response, "Website Redesign")

    def test_never_returns_another_users_project(self):
        Project.objects.create(user=self.other, name="Website Redesign")
        response = self.client.get(reverse("search:global"), {"q": "redesign"})
        self.assertNotContains(response, "Website Redesign")

    def test_no_project_results_when_the_feature_is_off(self):
        RoleFeatureToggle.objects.create(role=User.Role.USER, feature_key="projects", is_enabled=False)
        Project.objects.create(user=self.user, name="Website Redesign")
        response = self.client.get(reverse("search:global"), {"q": "redesign"})
        self.assertNotContains(response, "Website Redesign")


class UserSearchAuthorizationTests(TestCase):
    """Users are only ever a searchable category for Admin/SuperAdmin - and an Admin's results stay
    inside their own department, same as the existing admin Users list (_scope_users)."""

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")
        self.admin_a = User.objects.create_user(
            email="admin_a@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.dept_a
        )
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN
        )
        self.plain_user = User.objects.create_user(email="plain@example.com", password="pw12345!")
        self.member_a = User.objects.create_user(
            email="member.alpha@example.com", password="pw12345!", department=self.dept_a
        )
        self.member_b = User.objects.create_user(
            email="member.beta@example.com", password="pw12345!", department=self.dept_b
        )

    def test_a_regular_user_gets_no_users_category_at_all(self):
        self.client.login(email="plain@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": "member"})
        self.assertNotContains(response, "member.alpha@example.com")
        self.assertNotContains(response, "member.beta@example.com")

    def test_a_department_scoped_admin_only_sees_their_own_department(self):
        self.client.login(email="admin_a@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": "member"})
        self.assertContains(response, "member.alpha@example.com")
        self.assertNotContains(response, "member.beta@example.com")

    def test_superadmin_sees_every_department(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": "member"})
        self.assertContains(response, "member.alpha@example.com")
        self.assertContains(response, "member.beta@example.com")


class InvoiceSearchAuthorizationTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")
        self.plan = Plan.objects.create(name="Growth")
        self.admin_a = User.objects.create_user(
            email="admin_a@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.dept_a
        )
        self.recipient = User.objects.create_user(email="recipient@example.com", password="pw12345!")
        self.other_recipient = User.objects.create_user(email="other_recipient@example.com", password="pw12345!")
        self.invoice_own = make_invoice(self.recipient, self.plan)
        self.invoice_dept_a = make_invoice(self.other_recipient, self.plan, department=self.dept_a)
        self.invoice_dept_b = make_invoice(self.other_recipient, self.plan, department=self.dept_b)

    def test_a_regular_user_only_finds_their_own_invoice(self):
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": self.invoice_own.invoice_number})
        self.assertContains(response, self.invoice_own.invoice_number)

    def test_a_regular_user_never_finds_someone_elses_invoice_by_number(self):
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": self.invoice_dept_a.invoice_number})
        # Not a bare substring check: the "no results" empty-state message itself echoes the query
        # text (including the invoice number typed), so the real assertion is that no row LINKS to
        # that invoice's own detail page - the actual authorization boundary.
        self.assertNotContains(
            response, reverse("billing:invoice_detail", kwargs={"invoice_id": self.invoice_dept_a.id})
        )

    def test_an_admin_finds_their_own_departments_invoice(self):
        self.client.login(email="admin_a@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": self.invoice_dept_a.invoice_number})
        self.assertContains(response, self.invoice_dept_a.invoice_number)

    def test_an_admin_never_finds_another_departments_invoice(self):
        self.client.login(email="admin_a@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": self.invoice_dept_b.invoice_number})
        self.assertNotContains(
            response, reverse("billing:invoice_detail", kwargs={"invoice_id": self.invoice_dept_b.id})
        )


class MediaSearchAuthorizationTests(TestCase):
    """Media is a SuperAdmin-only searchable category (governance.media_views.MediaDashboardView's
    own restriction, reused as-is here by only ever populating this category for a SuperAdmin)."""

    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN
        )
        self.admin = User.objects.create_user(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN)
        self.plain_user = User.objects.create_user(email="plain@example.com", password="pw12345!")

    def test_a_regular_user_gets_no_media_category(self):
        self.client.login(email="plain@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": "anything"})
        self.assertNotIn("media_items", response.context)

    def test_a_plain_admin_gets_no_media_category(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": "anything"})
        self.assertNotIn("media_items", response.context)

    def test_a_superadmin_gets_the_media_category(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("search:global"), {"q": "anything"})
        self.assertIn("media_items", response.context)


class CommandPaletteVisibilityTests(TestCase):
    """The static "Pages" commands rendered straight into base.html - each one gated by the exact
    same permission check used everywhere else that link is offered (nav, profile dropdown, etc.)."""

    def setUp(self):
        self.dept = Department.objects.create(name="Dept A")
        self.plain_user = User.objects.create_user(email="plain@example.com", password="pw12345!")
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.dept
        )
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN
        )

    def test_the_palette_and_ctrl_k_trigger_render_for_a_default_role(self):
        self.client.login(email="plain@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, 'id="globalSearchOverlay"')
        self.assertContains(response, "portalOpenGlobalSearch")

    def test_a_regular_user_does_not_see_the_users_or_media_commands(self):
        self.client.login(email="plain@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertNotContains(response, reverse("governance:users"))
        self.assertNotContains(response, reverse("governance:media"))

    def test_an_admin_sees_the_users_command_but_not_media(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, reverse("governance:users"))
        self.assertNotContains(response, reverse("governance:media"))

    def test_a_superadmin_sees_both_the_users_and_media_commands(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, reverse("governance:users"))
        self.assertContains(response, reverse("governance:media"))

    def test_turning_the_feature_off_hides_the_whole_palette_and_triggers(self):
        RoleFeatureToggle.objects.create(role=User.Role.USER, feature_key="quick_switcher", is_enabled=False)
        self.client.login(email="plain@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:dashboard"))
        # The palette's own markup, and both header trigger buttons, are all gated on the same
        # {% if %} - the keydown listener in base.html still references portalOpenGlobalSearch by
        # name (it no-ops safely when globalSearchOverlay doesn't exist), so that name alone isn't
        # the right signal; the button/overlay markup is.
        self.assertNotContains(response, 'id="globalSearchOverlay"')
        self.assertNotContains(response, 'class="global-search-trigger"')
        self.assertNotContains(response, 'class="mobile-search-btn"')
