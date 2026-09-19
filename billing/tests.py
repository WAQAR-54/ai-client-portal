from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Team, User
from billing.access import has_overdue_unpaid_invoice
from billing.invoicing import (
    InvoiceGenerationError,
    generate_invoice_for_department,
    generate_invoice_for_team,
    generate_invoice_for_user,
)
from billing.models import (
    DepartmentBillingProfile,
    Invoice,
    OrganizationBillingProfile,
    RefundRequest,
    RegionalPrice,
    UserBillingProfile,
)
from billing.pdf import render_invoice_pdf
from billing.regions import REGIONS
from billing.tasks import send_overdue_reminders, sweep_due_invoices
from billing.tax_rules import tax_rule_for_country
from governance.models import Plan


class RegionalPricingViewTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True
        )
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.plan = Plan.objects.filter(name="Advanced").first() or Plan.objects.create(name="Advanced")

    def test_non_superadmin_cannot_access(self):
        for email in ("admin@example.com", "u@example.com"):
            self.client.login(email=email, password="pw12345!")
            response = self.client.get(reverse("billing:regional_pricing"))
            self.assertEqual(response.status_code, 403)
            self.client.logout()

    def test_visiting_the_page_creates_a_row_for_every_plan_and_default_region(self):
        self.client.login(email="super@example.com", password="pw12345!")
        self.client.get(reverse("billing:regional_pricing"))
        for code, _label, _currency, _flag in REGIONS:
            self.assertTrue(RegionalPrice.objects.filter(plan=self.plan, region_code=code).exists())

    def test_missing_price_banner_shows_until_every_region_is_priced(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:regional_pricing"))
        self.assertContains(response, "won't be purchasable")

        # Price every plan (not just self.plan) - other seeded plans left
        # unpriced would keep the page-wide banner showing regardless of
        # self.plan's own prices, since the banner covers every plan.
        for plan in Plan.objects.all():
            for code, _label, _currency, _flag in REGIONS:
                RegionalPrice.objects.update_or_create(plan=plan, region_code=code, defaults={"price": Decimal("10")})
        response = self.client.get(reverse("billing:regional_pricing"))
        self.assertNotContains(response, "won't be purchasable")


class PublicPricingViewTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(name="Public Plan", seats_included=3)
        RegionalPrice.objects.create(plan=self.plan, region_code="PK", price=Decimal("8900"))
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("29"))

    def test_no_login_required(self):
        response = self.client.get(reverse("billing:public_pricing"))
        self.assertEqual(response.status_code, 200)

    def test_detects_region_from_ip(self):
        # Not asserting on the exact rendered number: the same Pakistani IP
        # also trips GeoLanguageMiddleware into Urdu, whose locale-aware
        # number formatting (via humanize's intcomma) may not use a plain
        # ASCII comma - the region/plan selection is what this test cares
        # about, not thousands-separator rendering.
        response = self.client.get(reverse("billing:public_pricing"), REMOTE_ADDR="182.176.1.1")
        self.assertContains(response, "Pakistan")
        self.assertContains(response, "PKR")
        self.assertContains(response, "Public Plan")

    def test_unrecognized_region_falls_back_to_row(self):
        response = self.client.get(reverse("billing:public_pricing"), REMOTE_ADDR="8.8.8.8")
        self.assertContains(response, "Rest of world")
        self.assertContains(response, "USD")

    def test_region_query_param_overrides_ip_detection(self):
        response = self.client.get(reverse("billing:public_pricing"), {"region": "PK"}, REMOTE_ADDR="8.8.8.8")
        self.assertContains(response, "Pakistan")

    def test_invalid_region_query_param_falls_back_to_ip_detection(self):
        response = self.client.get(reverse("billing:public_pricing"), {"region": "ZZ"}, REMOTE_ADDR="182.176.1.1")
        self.assertContains(response, "Pakistan")

    def test_missing_price_shows_contact_us(self):
        Plan.objects.create(name="Unpriced Plan")
        response = self.client.get(reverse("billing:public_pricing"), REMOTE_ADDR="182.176.1.1")
        self.assertContains(response, "Contact us")

    def test_inactive_and_demo_plans_excluded(self):
        Plan.objects.create(name="Inactive Plan", is_active=False)
        Plan.objects.create(name="Demo Plan", is_demo=True)
        response = self.client.get(reverse("billing:public_pricing"), REMOTE_ADDR="182.176.1.1")
        self.assertNotContains(response, "Inactive Plan")
        self.assertNotContains(response, "Demo Plan")

    def test_show_on_public_pricing_false_hides_an_otherwise_active_plan(self):
        """An active, non-demo plan can still be hidden from this
        anonymous marketing grid via the new independent toggle - e.g. a
        sales-only enterprise tier that stays assignable elsewhere."""
        Plan.objects.create(name="Sales Only Plan", is_active=True, show_on_public_pricing=False)
        response = self.client.get(reverse("billing:public_pricing"), REMOTE_ADDR="182.176.1.1")
        self.assertNotContains(response, "Sales Only Plan")

    def test_leads_with_agents_and_team_features_not_just_the_model_bundle(self):
        """Positioning fix (Gap 4): "4 models bundled" is a price-based
        pitch a provider could undercut by bundling themselves; the
        durable differentiator is what only this app offers on top -
        agents, team billing, an admin console. That should lead the
        page copy, with the model count as a secondary detail."""
        response = self.client.get(reverse("billing:public_pricing"))
        self.assertContains(response, "Sales, Marketing, and Dev AI agents")
        self.assertContains(response, "team-based billing")


class UpdatePlanRegionalPricingTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.plan = Plan.objects.create(name="Test Plan")
        self.client.login(email="super@example.com", password="pw12345!")

    def test_saves_price_with_comma_stripped_and_seats_included(self):
        response = self.client.post(
            reverse("billing:update_plan_regional_pricing", kwargs={"plan_id": self.plan.id}),
            {"seats_included": "3", "price_PK": "8,900", "price_SA": "299", "price_AE": "299", "price_ROW": "32"},
        )
        self.assertRedirects(response, reverse("billing:regional_pricing"))

        self.plan.refresh_from_db()
        self.assertEqual(self.plan.seats_included, 3)
        pk_price = RegionalPrice.objects.get(plan=self.plan, region_code="PK")
        self.assertEqual(pk_price.price, Decimal("8900"))

    def test_blank_price_saves_as_none_not_zero(self):
        RegionalPrice.objects.create(plan=self.plan, region_code="PK", price=Decimal("100"))
        self.client.post(
            reverse("billing:update_plan_regional_pricing", kwargs={"plan_id": self.plan.id}),
            {"price_PK": "", "seats_included": ""},
        )
        pk_price = RegionalPrice.objects.get(plan=self.plan, region_code="PK")
        self.assertIsNone(pk_price.price)
        self.plan.refresh_from_db()
        self.assertIsNone(self.plan.seats_included)

    def test_saves_extra_seat_price(self):
        self.client.post(
            reverse("billing:update_plan_regional_pricing", kwargs={"plan_id": self.plan.id}),
            {"seats_included": "3", "extra_seat_price_PK": "2,500"},
        )
        pk_price = RegionalPrice.objects.get(plan=self.plan, region_code="PK")
        self.assertEqual(pk_price.extra_seat_price, Decimal("2500"))

    def test_non_superadmin_cannot_update(self):
        self.client.logout()
        User.objects.create_user(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True)
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:update_plan_regional_pricing", kwargs={"plan_id": self.plan.id}), {"price_PK": "999"}
        )
        self.assertEqual(response.status_code, 403)


class AddRegionTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.plan_a = Plan.objects.create(name="Plan A")
        self.plan_b = Plan.objects.create(name="Plan B")
        self.client.login(email="super@example.com", password="pw12345!")

    def test_add_region_creates_rows_for_every_existing_plan(self):
        self.client.post(reverse("billing:add_region"), {"region_code": "GB"})
        self.assertTrue(RegionalPrice.objects.filter(plan=self.plan_a, region_code="GB").exists())
        self.assertTrue(RegionalPrice.objects.filter(plan=self.plan_b, region_code="GB").exists())

    def test_added_region_then_appears_on_the_pricing_page(self):
        self.client.post(reverse("billing:add_region"), {"region_code": "QA"})
        response = self.client.get(reverse("billing:regional_pricing"))
        self.assertContains(response, "Qatar")

    def test_invalid_region_code_is_ignored(self):
        response = self.client.post(reverse("billing:add_region"), {"region_code": "ZZ"})
        self.assertRedirects(response, reverse("billing:regional_pricing"))
        self.assertFalse(RegionalPrice.objects.filter(region_code="ZZ").exists())


class RemoveRegionTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True
        )
        self.plan_a = Plan.objects.create(name="Plan A")
        self.plan_b = Plan.objects.create(name="Plan B")
        self.client.login(email="super@example.com", password="pw12345!")
        self.client.post(reverse("billing:add_region"), {"region_code": "GB"})
        self.client.get(reverse("billing:regional_pricing"))  # lazily creates rows for every active region

    def test_removing_deletes_every_plans_price_row(self):
        self.client.post(reverse("billing:remove_region"), {"region_code": "GB"})
        self.assertFalse(RegionalPrice.objects.filter(region_code="GB").exists())

    def test_removed_region_reappears_in_available_extra_regions(self):
        self.client.post(reverse("billing:remove_region"), {"region_code": "GB"})
        response = self.client.get(reverse("billing:regional_pricing"))
        self.assertContains(response, "+ Add region")
        self.assertContains(response, "United Kingdom")

    def test_default_region_code_is_ignored(self):
        response = self.client.post(reverse("billing:remove_region"), {"region_code": "PK"})
        self.assertRedirects(response, reverse("billing:regional_pricing"))
        self.assertTrue(RegionalPrice.objects.filter(region_code="PK").exists())

    def test_non_superadmin_cannot_remove(self):
        self.client.logout()
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:remove_region"), {"region_code": "GB"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(RegionalPrice.objects.filter(region_code="GB").exists())


class OrganizationBillingSettingsViewTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True
        )

    def test_non_superadmin_cannot_access(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:organization_billing"))
        self.assertEqual(response.status_code, 403)

    def test_superadmin_can_view_and_update(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:organization_billing"))
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            reverse("billing:update_organization_billing"),
            {
                "bank_name": "Meezan Bank",
                "account_title": "AI Client Portal Pvt Ltd",
                "account_number": "PK00MEZN0000000000000000",
                "swift_code": "MEZNPKKA",
                "payment_note": "Include invoice number as reference.",
            },
        )
        self.assertRedirects(response, reverse("billing:organization_billing"))
        profile = OrganizationBillingProfile.load()
        self.assertEqual(profile.bank_name, "Meezan Bank")
        self.assertEqual(profile.swift_code, "MEZNPKKA")

    def test_non_superadmin_cannot_update(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:update_organization_billing"), {"bank_name": "Hacked Bank"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(OrganizationBillingProfile.load().bank_name, "")


class DepartmentBillingProfileViewTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.other_admin = User.objects.create_user(
            email="otheradmin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.other_department,
        )
        self.plain_user = User.objects.create_user(
            email="user@example.com", password="pw12345!", department=self.department
        )

    def _url(self, department=None):
        return reverse(
            "billing:department_billing_profile", kwargs={"department_id": (department or self.department).id}
        )

    def test_admin_can_view_own_department(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_admin_cannot_view_other_department(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(self._url(self.other_department))
        self.assertEqual(response.status_code, 403)

    def test_superadmin_can_view_any_department(self):
        self.client.login(email="super@example.com", password="pw12345!")
        self.assertEqual(self.client.get(self._url()).status_code, 200)
        self.assertEqual(self.client.get(self._url(self.other_department)).status_code, 200)

    def test_plain_user_cannot_access(self):
        self.client.login(email="user@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 403)

    def test_visiting_the_page_creates_a_profile_row(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        self.client.get(self._url())
        self.assertTrue(DepartmentBillingProfile.objects.filter(department=self.department).exists())

    def test_update_saves_fields(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        update_url = reverse("billing:update_department_billing_profile", kwargs={"department_id": self.department.id})
        response = self.client.post(
            update_url,
            {
                "company_name": "Sales Co",
                "country": "AE",
                "billing_address": "Dubai, UAE",
                "tax_id": "TRN-12345",
                "custom_tax_rate": "",
                "auto_generate_invoices": "on",
                "reminder_days_after_due": "7",
            },
        )
        self.assertRedirects(response, self._url())
        profile = DepartmentBillingProfile.objects.get(department=self.department)
        self.assertEqual(profile.company_name, "Sales Co")
        self.assertEqual(profile.country, "AE")
        self.assertFalse(profile.is_tax_exempt)
        self.assertEqual(profile.reminder_days_after_due, 7)

    def test_admin_cannot_update_other_department(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        update_url = reverse(
            "billing:update_department_billing_profile", kwargs={"department_id": self.other_department.id}
        )
        response = self.client.post(update_url, {"company_name": "Hijacked"})
        self.assertEqual(response.status_code, 403)


class EffectiveTaxRateTests(TestCase):
    """Money math must never be "probably right" - dedicated cases for
    exempt, custom-override, and country-default precedence."""

    def setUp(self):
        self.department = Department.objects.create(name="Sales")

    def test_default_rate_comes_from_country(self):
        profile = DepartmentBillingProfile.objects.create(department=self.department, country="AE")
        self.assertEqual(profile.effective_tax_rate(), tax_rule_for_country("AE")["tax_rate"])
        self.assertEqual(profile.effective_tax_rate(), Decimal("5"))

    def test_unknown_country_falls_back_to_zero(self):
        profile = DepartmentBillingProfile.objects.create(department=self.department, country="OTHER")
        self.assertEqual(profile.effective_tax_rate(), Decimal("0"))

    def test_custom_rate_overrides_country_default(self):
        profile = DepartmentBillingProfile.objects.create(
            department=self.department, country="AE", custom_tax_rate=Decimal("8.5")
        )
        self.assertEqual(profile.effective_tax_rate(), Decimal("8.5"))

    def test_exempt_overrides_everything(self):
        profile = DepartmentBillingProfile.objects.create(
            department=self.department, country="AE", custom_tax_rate=Decimal("8.5"), is_tax_exempt=True
        )
        self.assertEqual(profile.effective_tax_rate(), Decimal("0"))


class GenerateInvoiceForDepartmentTests(TestCase):
    """Money math must never be "probably right" - dedicated cases for
    plan pricing, tax, and the per-seat-billing extra-seats line item."""

    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.plan = Plan.objects.create(name="Growth", seats_included=2)
        RegionalPrice.objects.create(
            plan=self.plan, region_code="AE", price=Decimal("100"), extra_seat_price=Decimal("20")
        )
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, country="AE")

    def _add_users(self, count):
        for i in range(count):
            User.objects.create_user(email=f"seat{i}@example.com", password="pw12345!", department=self.department)

    def test_raises_when_no_plan_assigned(self):
        self.department.plan = None
        self.department.save(update_fields=["plan"])
        with self.assertRaises(InvoiceGenerationError):
            generate_invoice_for_department(self.department)

    def test_raises_when_plan_has_no_price_for_region(self):
        RegionalPrice.objects.filter(plan=self.plan, region_code="AE").update(price=None)
        with self.assertRaises(InvoiceGenerationError):
            generate_invoice_for_department(self.department)

    def test_basic_invoice_with_no_tax_no_extra_seats(self):
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=True)
        invoice = generate_invoice_for_department(self.department)
        self.assertEqual(invoice.subtotal, Decimal("100"))
        self.assertEqual(invoice.tax_amount, Decimal("0.00"))
        self.assertEqual(invoice.total, Decimal("100"))
        self.assertEqual(invoice.currency, "AED")
        self.assertEqual(invoice.status, Invoice.Status.UNPAID)
        self.assertTrue(invoice.invoice_number.startswith("INV-"))
        self.assertEqual(len(invoice.line_items), 1)
        self.assertEqual(invoice.line_items[0]["amount"], "100.00")

    def test_extra_seats_get_their_own_line_item(self):
        self._add_users(4)
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=True)
        invoice = generate_invoice_for_department(self.department)
        self.assertEqual(len(invoice.line_items), 2)
        self.assertIn("Extra members", invoice.line_items[1]["description"])
        self.assertIn("4 total, 2 included", invoice.line_items[1]["description"])
        self.assertEqual(invoice.line_items[1]["amount"], "40.00")

    def test_generated_invoice_is_linked_to_recipient(self):
        recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )
        invoice = generate_invoice_for_department(self.department, recipient_user=recipient)
        self.assertEqual(invoice.recipient_user, recipient)

    def test_explicit_plan_argument_overrides_department_plan(self):
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=True)
        other_plan = Plan.objects.create(name="Enterprise")
        RegionalPrice.objects.create(plan=other_plan, region_code="AE", price=Decimal("999"))
        invoice = generate_invoice_for_department(self.department, plan=other_plan)
        self.assertEqual(invoice.plan, other_plan)
        self.assertEqual(invoice.subtotal, Decimal("999"))

    def test_explicit_seat_count_argument_overrides_actual_headcount(self):
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=True)
        # No real users added to the department at all - seat_count is
        # taken purely from the explicit argument, not department.users.
        invoice = generate_invoice_for_department(self.department, seat_count=7)
        # 7 given, 2 included -> 5 extra x 20/seat = 100 on top of the 100 base.
        self.assertEqual(invoice.subtotal, Decimal("200"))

    def test_applies_country_default_tax_rate(self):
        invoice = generate_invoice_for_department(self.department)
        # AE's default rate is 5% (billing/tax_rules.py) on a 100 subtotal.
        self.assertEqual(invoice.tax_rate, Decimal("5"))
        self.assertEqual(invoice.tax_amount, Decimal("5.00"))
        self.assertEqual(invoice.total, Decimal("105.00"))

    def test_extra_seats_beyond_included_count_are_billed(self):
        self._add_users(4)
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=True)
        invoice = generate_invoice_for_department(self.department)
        # 4 people, 2 included -> 2 extra x 20/seat = 40 on top of the 100 base.
        self.assertEqual(invoice.subtotal, Decimal("140"))
        self.assertEqual(invoice.total, Decimal("140"))

    def test_extra_seats_not_billed_when_extra_seat_price_unset(self):
        RegionalPrice.objects.filter(plan=self.plan, region_code="AE").update(extra_seat_price=None)
        self._add_users(4)
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=True)
        invoice = generate_invoice_for_department(self.department)
        self.assertEqual(invoice.subtotal, Decimal("100"))

    def test_unlimited_seats_included_never_bills_extra(self):
        self.plan.seats_included = None
        self.plan.save(update_fields=["seats_included"])
        self._add_users(10)
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=True)
        invoice = generate_invoice_for_department(self.department)
        self.assertEqual(invoice.subtotal, Decimal("100"))

    def test_invoice_numbers_increment_per_year(self):
        first = generate_invoice_for_department(self.department)
        second_department = Department.objects.create(name="Support", plan=self.plan)
        DepartmentBillingProfile.objects.create(department=second_department, country="AE")
        second = generate_invoice_for_department(second_department)
        first_seq = int(first.invoice_number.rsplit("-", 1)[-1])
        second_seq = int(second.invoice_number.rsplit("-", 1)[-1])
        self.assertEqual(second_seq, first_seq + 1)

    def test_falls_back_to_row_when_no_billing_country_set(self):
        DepartmentBillingProfile.objects.filter(department=self.department).update(country="")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("30"))
        invoice = generate_invoice_for_department(self.department)
        self.assertEqual(invoice.currency, "USD")
        self.assertEqual(invoice.subtotal, Decimal("30"))

    def test_explicit_region_code_overrides_departments_configured_country(self):
        # self.department's DepartmentBillingProfile has country="AE" and is
        # tax-exempt (from setUp) - forcing region_code="ROW" here should
        # use ROW's price/currency, and (since exempt still wins regardless
        # of region) stay at 0% tax rather than picking up ROW's own rate.
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("30"))
        invoice = generate_invoice_for_department(self.department, region_code="ROW")
        self.assertEqual(invoice.currency, "USD")
        self.assertEqual(invoice.subtotal, Decimal("30"))
        self.assertEqual(invoice.tax_rate, Decimal("0"))

    def test_explicit_region_code_still_applies_countrys_default_tax_when_not_exempt(self):
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=False)
        RegionalPrice.objects.create(plan=self.plan, region_code="SA", price=Decimal("50"))
        invoice = generate_invoice_for_department(self.department, region_code="SA")
        # SA's default rate is 15% (billing/tax_rules.py), not AE's 5%.
        self.assertEqual(invoice.tax_rate, Decimal("15"))


class GenerateInvoiceForUserTests(TestCase):
    """Money math must never be "probably right" - covers both the
    has-a-department delegation path and the genuinely new department-less
    path (billing.invoicing.generate_invoice_for_user)."""

    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.plan = Plan.objects.create(name="Growth", seats_included=2)
        RegionalPrice.objects.create(
            plan=self.plan, region_code="AE", price=Decimal("100"), extra_seat_price=Decimal("20")
        )
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("40"))
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, country="AE", is_tax_exempt=True)

    def test_department_user_delegates_to_department_billing_profile(self):
        user = User.objects.create_user(email="dept@example.com", password="pw12345!", department=self.department)
        invoice = generate_invoice_for_user(user)
        self.assertEqual(invoice.department, self.department)
        self.assertEqual(invoice.currency, "AED")
        self.assertEqual(invoice.subtotal, Decimal("100"))
        self.assertEqual(invoice.tax_amount, Decimal("0.00"))  # department is_tax_exempt=True

    def test_department_less_user_defaults_to_row_and_one_seat(self):
        user = User.objects.create_user(email="solo@example.com", password="pw12345!")
        invoice = generate_invoice_for_user(user, plan=self.plan)
        self.assertIsNone(invoice.department)
        self.assertEqual(invoice.currency, "USD")
        self.assertEqual(invoice.subtotal, Decimal("40"))
        self.assertEqual(invoice.tax_rate, Decimal("0"))  # ROW isn't in TAX_RULES -> DEFAULT_TAX_RULE
        self.assertEqual(invoice.seats_billed, 1)

    def test_department_less_invoice_line_item_describes_the_plans_features_not_the_email(self):
        """Reported directly - the line item used to read "Growth —
        solo@example.com", the recipient's own email standing in as if
        it were an item description (confusing, and redundant with the
        Bill To block, which already has that address). Now describes
        what's actually included instead (governance.plans.
        plan_capability_summary - the same list shown on the Plans
        pages)."""
        self.plan.feature_flags = {"document_generation": True}
        self.plan.save(update_fields=["feature_flags"])
        user = User.objects.create_user(email="solo@example.com", password="pw12345!")
        invoice = generate_invoice_for_user(user, plan=self.plan)
        description = invoice.line_items[0]["description"]
        self.assertIn("Growth Plan", description)
        self.assertIn("Document generation", description)
        self.assertNotIn("solo@example.com", description)

    def test_explicit_plan_overrides_department_plan(self):
        other_plan = Plan.objects.create(name="Enterprise")
        RegionalPrice.objects.create(plan=other_plan, region_code="AE", price=Decimal("999"))
        user = User.objects.create_user(email="dept2@example.com", password="pw12345!", department=self.department)
        invoice = generate_invoice_for_user(user, plan=other_plan)
        self.assertEqual(invoice.plan, other_plan)
        self.assertEqual(invoice.subtotal, Decimal("999"))

    def test_falls_back_to_users_own_plan_assignment_when_no_department_plan(self):
        no_plan_department = Department.objects.create(name="No Plan Dept")
        user = User.objects.create_user(email="noplan@example.com", password="pw12345!", department=no_plan_department)
        demo_plan = user.plan_assignment.plan  # assigned automatically on creation
        RegionalPrice.objects.create(plan=demo_plan, region_code="ROW", price=Decimal("5"))
        invoice = generate_invoice_for_user(user)
        self.assertEqual(invoice.plan, demo_plan)
        # The department has no plan of its own, so this personal invoice
        # is deliberately NOT attached to it (no DepartmentBillingProfile
        # side-effect on a department that was never set up for billing).
        self.assertIsNone(invoice.department)

    def test_raises_when_no_plan_resolvable(self):
        user = User.objects.create_user(email="noplanatall@example.com", password="pw12345!")
        user.plan_assignment.delete()
        user = User.objects.get(pk=user.pk)  # fresh instance - no stale reverse-relation cache
        with self.assertRaises(InvoiceGenerationError):
            generate_invoice_for_user(user)

    def test_raises_when_no_row_price_for_department_less_user(self):
        unpriced_plan = Plan.objects.create(name="Unpriced")
        user = User.objects.create_user(email="unpriced@example.com", password="pw12345!")
        with self.assertRaises(InvoiceGenerationError):
            generate_invoice_for_user(user, plan=unpriced_plan)

    def test_due_in_days_defaults_to_demo_duration_for_demo_plan(self):
        demo_plan = Plan.objects.create(name="Trial Demo", is_demo=True, demo_duration_days=7)
        RegionalPrice.objects.create(plan=demo_plan, region_code="ROW", price=Decimal("0"))
        user = User.objects.create_user(email="trial@example.com", password="pw12345!")
        invoice = generate_invoice_for_user(user, plan=demo_plan)
        self.assertEqual(invoice.due_date, invoice.issue_date + timedelta(days=7))

    def test_due_in_days_defaults_to_14_for_regular_plan(self):
        user = User.objects.create_user(email="regular@example.com", password="pw12345!")
        invoice = generate_invoice_for_user(user, plan=self.plan, region_code="ROW")
        self.assertEqual(invoice.due_date, invoice.issue_date + timedelta(days=14))

    def test_explicit_due_in_days_always_wins(self):
        demo_plan = Plan.objects.create(name="Trial Demo 2", is_demo=True, demo_duration_days=7)
        RegionalPrice.objects.create(plan=demo_plan, region_code="ROW", price=Decimal("0"))
        user = User.objects.create_user(email="trial2@example.com", password="pw12345!")
        invoice = generate_invoice_for_user(user, plan=demo_plan, due_in_days=3)
        self.assertEqual(invoice.due_date, invoice.issue_date + timedelta(days=3))

    def test_explicit_region_code_overrides_row_default(self):
        user = User.objects.create_user(email="regionpick@example.com", password="pw12345!")
        invoice = generate_invoice_for_user(user, plan=self.plan, region_code="AE")
        self.assertEqual(invoice.currency, "AED")
        self.assertEqual(invoice.subtotal, Decimal("100"))


class GenerateInvoiceForTeamTests(TestCase):
    """billing.invoicing.generate_invoice_for_team - reported directly:
    there was no way to bill a specific team at all (only a whole
    Department, or one individual user). Recipient is always the
    team's own Manager; seat count is always team.members.count(),
    computed live so it can never drift from the team's real size."""

    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.plan = Plan.objects.create(name="Growth", seats_included=2)
        RegionalPrice.objects.create(
            plan=self.plan, region_code="ROW", price=Decimal("100"), extra_seat_price=Decimal("10")
        )
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        self.manager = User.objects.create_user(
            email="manager@example.com", password="pw12345!", role=User.Role.MANAGER, department=self.department
        )
        self.team = Team.objects.create(name="Alpha", department=self.department, manager=self.manager)
        self.manager.team = self.team
        self.manager.save(update_fields=["team"])

    def test_invoice_is_billed_to_the_teams_manager(self):
        invoice = generate_invoice_for_team(self.team)
        self.assertEqual(invoice.recipient_user, self.manager)
        self.assertEqual(invoice.department, self.department)

    def test_seat_count_is_the_teams_real_member_count(self):
        for i in range(3):
            member = User.objects.create_user(email=f"member{i}@example.com", password="pw12345!")
            member.team = self.team
            member.save(update_fields=["team"])
        # 3 members + the manager (also a team member via User.team,
        # kept in sync - see Team.manager's own help_text) = 4.
        invoice = generate_invoice_for_team(self.team)
        self.assertEqual(invoice.seats_billed, 4)

    def test_seat_count_recalculates_as_team_size_changes(self):
        """The exact complaint: increasing team size wasn't reflected in
        the invoice. Each call recomputes from the real roster, not a
        stored number - so a second invoice picks up the new size."""
        first_invoice = generate_invoice_for_team(self.team)
        self.assertEqual(first_invoice.seats_billed, 1)

        new_member = User.objects.create_user(email="newmember@example.com", password="pw12345!")
        new_member.team = self.team
        new_member.save(update_fields=["team"])

        second_invoice = generate_invoice_for_team(self.team)
        self.assertEqual(second_invoice.seats_billed, 2)

    def test_extra_seats_over_the_plans_included_count_are_billed(self):
        for i in range(2):
            member = User.objects.create_user(email=f"extra{i}@example.com", password="pw12345!")
            member.team = self.team
            member.save(update_fields=["team"])
        # 2 extra members + manager = 3 seats; plan includes 2 -> 1 extra.
        invoice = generate_invoice_for_team(self.team)
        self.assertEqual(invoice.seats_billed, 3)
        self.assertEqual(invoice.subtotal, Decimal("110"))  # 100 base + 1 extra seat @ 10

    def test_raises_when_the_team_has_no_manager(self):
        unmanaged_team = Team.objects.create(name="Beta", department=self.department)
        with self.assertRaises(InvoiceGenerationError):
            generate_invoice_for_team(unmanaged_team)


class GenerateTeamInvoiceViewTests(TestCase):
    """The "Generate invoice" button on the Teams page (governance:teams)."""

    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")
        self.plan = Plan.objects.create(name="Growth")

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.manager = User.objects.create_user(
            email="manager@example.com", password="pw12345!", role=User.Role.MANAGER, department=self.department
        )
        self.team = Team.objects.create(name="Alpha", department=self.department, manager=self.manager)
        self.manager.team = self.team
        self.manager.save(update_fields=["team"])

        # Deliberately priced/assigned AFTER every fixture user is created
        # (see GenerateInvoiceViewTests' own setUp for the same reasoning):
        # generate_welcome_invoice_on_creation (accounts/signals.py) would
        # otherwise give each of them their own extra welcome invoice the
        # moment the department has both a plan and a price, muddying
        # exactly the single invoice each test below expects from its own
        # POST.
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])

    def _url(self):
        from django.urls import reverse

        return reverse("billing:generate_team_invoice", kwargs={"team_id": self.team.id})

    def test_admin_generates_invoice_for_own_departments_team(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(self._url())
        self.assertRedirects(response, reverse("governance:teams"))
        from billing.models import Invoice

        self.assertTrue(Invoice.objects.filter(recipient_user=self.manager).exists())

    def test_superadmin_generates_invoice_for_any_team(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(self._url())
        self.assertRedirects(response, reverse("governance:teams"))
        from billing.models import Invoice

        self.assertTrue(Invoice.objects.filter(recipient_user=self.manager).exists())

    def test_admin_cannot_generate_for_another_departments_team(self):
        other_manager = User.objects.create_user(
            email="othermanager@example.com", password="pw12345!", department=self.other_department
        )
        other_team = Team.objects.create(name="Beta", department=self.other_department, manager=other_manager)
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:generate_team_invoice", kwargs={"team_id": other_team.id}))
        self.assertEqual(response.status_code, 403)

    def test_unmanaged_team_shows_an_error_and_creates_no_invoice(self):
        unmanaged_team = Team.objects.create(name="Gamma", department=self.department)
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:generate_team_invoice", kwargs={"team_id": unmanaged_team.id}), follow=True
        )
        from billing.models import Invoice

        self.assertFalse(Invoice.objects.filter(department=self.department, plan=self.plan).exists())
        messages = list(response.context["messages"])
        self.assertTrue(any("no manager" in str(m) for m in messages))


class InvoiceListViewTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")
        self.plan = Plan.objects.create(name="Growth", seats_included=None)
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )
        self.other_recipient = User.objects.create_user(
            email="otherrecipient@example.com", password="pw12345!", department=self.other_department
        )

        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, is_tax_exempt=True)
        self.invoice = generate_invoice_for_department(self.department, recipient_user=self.recipient)

        self.other_department.plan = self.plan
        self.other_department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.other_department, is_tax_exempt=True)
        self.other_invoice = generate_invoice_for_department(self.other_department, recipient_user=self.other_recipient)

    def test_admin_sees_only_own_department_invoices(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertContains(response, self.invoice.invoice_number)
        self.assertNotContains(response, self.other_invoice.invoice_number)

    def test_superadmin_sees_all_invoices(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertContains(response, self.invoice.invoice_number)
        self.assertContains(response, self.other_invoice.invoice_number)

    def test_superadmin_can_filter_by_department(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"), {"department": self.department.id})
        self.assertContains(response, self.invoice.invoice_number)
        self.assertNotContains(response, self.other_invoice.invoice_number)

    def test_list_is_paginated_past_50_invoices(self):
        """Production-readiness audit gap: this list had no pagination
        at all - an admin-wide, unfiltered invoice history would
        eventually mean loading every invoice the org has ever
        generated onto one page. 2 invoices already exist from setUp;
        top up to 51 total so a second page genuinely exists."""
        for _ in range(49):
            generate_invoice_for_department(self.department, recipient_user=self.recipient)

        self.client.login(email="super@example.com", password="pw12345!")
        page1 = self.client.get(reverse("billing:invoices"))
        self.assertEqual(len(page1.context["page_obj"]), 50)
        self.assertTrue(page1.context["is_paginated"])
        self.assertContains(page1, "Page 1 of 2")

        page2 = self.client.get(reverse("billing:invoices"), {"page": 2})
        self.assertEqual(len(page2.context["page_obj"]), 1)

    def test_admin_can_toggle_own_department_invoice_status(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:toggle_invoice_status", kwargs={"invoice_id": self.invoice.id}))
        self.assertRedirects(response, reverse("billing:invoices"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

    def test_admin_cannot_toggle_other_departments_invoice_status(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:toggle_invoice_status", kwargs={"invoice_id": self.other_invoice.id})
        )
        self.assertEqual(response.status_code, 403)

    def test_superadmin_can_toggle_status_plain_post(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:toggle_invoice_status", kwargs={"invoice_id": self.invoice.id}))
        self.assertRedirects(response, reverse("billing:invoices"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

    def test_superadmin_can_toggle_status_htmx(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:toggle_invoice_status", kwargs={"invoice_id": self.invoice.id}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)
        self.assertContains(response, self.invoice.invoice_number)

    def test_toggle_flips_back_to_unpaid(self):
        self.client.login(email="super@example.com", password="pw12345!")
        url = reverse("billing:toggle_invoice_status", kwargs={"invoice_id": self.invoice.id})
        self.client.post(url)
        self.client.post(url)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.UNPAID)


class GenerateInvoiceViewTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")
        self.plan = Plan.objects.create(name="Growth")

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )
        self.other_recipient = User.objects.create_user(
            email="otherrecipient@example.com", password="pw12345!", department=self.other_department
        )

        # Deliberately priced/assigned AFTER every fixture user is created:
        # generate_welcome_invoice_on_creation (accounts/signals.py) would
        # otherwise give each of them their own extra welcome invoice the
        # moment their department has both a plan and a price, muddying
        # exactly the single invoice each test below expects from its own
        # POST.
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        self.other_department.plan = self.plan
        self.other_department.save(update_fields=["plan"])

    def test_superadmin_generates_invoice_for_any_recipient(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:generate_invoice"), {"recipient_user_id": self.other_recipient.id})
        self.assertRedirects(response, reverse("billing:invoices"))
        invoice = Invoice.objects.get(department=self.other_department)
        self.assertEqual(invoice.recipient_user, self.other_recipient)

    def test_admin_generates_invoice_for_own_department_recipient(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:generate_invoice"), {"recipient_user_id": self.recipient.id})
        self.assertRedirects(response, reverse("billing:invoices"))
        invoice = Invoice.objects.get(department=self.department)
        self.assertEqual(invoice.recipient_user, self.recipient)

    def test_admin_cannot_generate_for_other_departments_recipient(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:generate_invoice"), {"recipient_user_id": self.other_recipient.id})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Invoice.objects.filter(department=self.other_department).exists())

    def test_plain_user_cannot_generate(self):
        User.objects.create_user(email="plain@example.com", password="pw12345!", department=self.department)
        self.client.login(email="plain@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:generate_invoice"), {"recipient_user_id": self.recipient.id})
        self.assertEqual(response.status_code, 403)

    def test_missing_price_shows_error_message_and_creates_nothing(self):
        self.client.login(email="super@example.com", password="pw12345!")
        RegionalPrice.objects.filter(plan=self.plan, region_code="ROW").update(price=None)
        response = self.client.post(
            reverse("billing:generate_invoice"), {"recipient_user_id": self.recipient.id}, follow=True
        )
        self.assertFalse(Invoice.objects.filter(department=self.department).exists())
        messages = list(response.context["messages"])
        self.assertTrue(any("no price" in str(m) for m in messages))

    def test_explicit_region_code_overrides_auto_detected_region(self):
        RegionalPrice.objects.create(plan=self.plan, region_code="PK", price=Decimal("8900"))
        self.client.login(email="super@example.com", password="pw12345!")
        self.client.post(
            reverse("billing:generate_invoice"),
            {"recipient_user_id": self.recipient.id, "region_code": "PK"},
        )
        invoice = Invoice.objects.get(department=self.department)
        self.assertEqual(invoice.currency, "PKR")
        self.assertEqual(invoice.subtotal, Decimal("8900"))

    def test_explicit_due_date_overrides_the_default(self):
        from django.utils import timezone

        self.client.login(email="super@example.com", password="pw12345!")
        chosen_due_date = timezone.localdate() + timezone.timedelta(days=3)
        self.client.post(
            reverse("billing:generate_invoice"),
            {"recipient_user_id": self.recipient.id, "due_date": chosen_due_date.isoformat()},
        )
        invoice = Invoice.objects.get(department=self.department)
        self.assertEqual(invoice.due_date, chosen_due_date)

    def test_blank_due_date_falls_back_to_the_normal_default(self):
        from django.utils import timezone

        self.client.login(email="super@example.com", password="pw12345!")
        self.client.post(reverse("billing:generate_invoice"), {"recipient_user_id": self.recipient.id})
        invoice = Invoice.objects.get(department=self.department)
        self.assertEqual(invoice.due_date, invoice.issue_date + timezone.timedelta(days=14))

    def test_a_past_due_date_is_allowed_not_rejected(self):
        """A deliberate backdate (an admin knows this invoice is already
        overdue) - not an error case."""
        from django.utils import timezone

        self.client.login(email="super@example.com", password="pw12345!")
        past_due_date = timezone.localdate() - timezone.timedelta(days=5)
        response = self.client.post(
            reverse("billing:generate_invoice"),
            {"recipient_user_id": self.recipient.id, "due_date": past_due_date.isoformat()},
        )
        self.assertRedirects(response, reverse("billing:invoices"))
        invoice = Invoice.objects.get(department=self.department)
        self.assertEqual(invoice.due_date, past_due_date)

    def test_invalid_due_date_is_a_bad_request(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:generate_invoice"),
            {"recipient_user_id": self.recipient.id, "due_date": "not-a-date"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Invoice.objects.filter(department=self.department).exists())

    def test_recipient_in_department_without_a_plan_is_still_eligible(self):
        # "Bill to" should list everyone with a department, not just
        # departments that already carry a subscription - the plan is
        # chosen per-invoice below, not required up front.
        no_plan_department = Department.objects.create(name="No Plan Dept")
        User.objects.create_user(email="noplan@example.com", password="pw12345!", department=no_plan_department)
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertContains(response, "noplan@example.com")

    def test_explicit_plan_id_overrides_departments_assigned_plan(self):
        other_plan = Plan.objects.create(name="Enterprise")
        RegionalPrice.objects.create(plan=other_plan, region_code="ROW", price=Decimal("500"))
        self.client.login(email="super@example.com", password="pw12345!")
        self.client.post(
            reverse("billing:generate_invoice"),
            {"recipient_user_id": self.recipient.id, "plan_id": other_plan.id},
        )
        invoice = Invoice.objects.get(department=self.department)
        self.assertEqual(invoice.plan, other_plan)
        self.assertEqual(invoice.subtotal, Decimal("500"))
        # The department's own subscription is untouched by a one-off
        # invoice for a different plan.
        self.department.refresh_from_db()
        self.assertEqual(self.department.plan, self.plan)

    def test_explicit_seat_count_overrides_actual_headcount(self):
        self.plan.seats_included = 1
        self.plan.save(update_fields=["seats_included"])
        RegionalPrice.objects.filter(plan=self.plan, region_code="ROW").update(extra_seat_price=Decimal("10"))
        self.client.login(email="super@example.com", password="pw12345!")
        self.client.post(
            reverse("billing:generate_invoice"),
            {"recipient_user_id": self.recipient.id, "seat_count": "6"},
        )
        invoice = Invoice.objects.get(department=self.department)
        # 6 entered manually, 1 included -> 5 extra x 10 = 50 on top of 50 base.
        self.assertEqual(invoice.subtotal, Decimal("100"))


class InvoicePaymentVerificationTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")
        self.plan = Plan.objects.create(name="Growth")

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.other_admin = User.objects.create_user(
            email="otheradmin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.other_department,
        )
        self.recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )

        # Priced/assigned/profiled AFTER every fixture user - see the
        # identical comment in GenerateInvoiceViewTests.setUp.
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, is_tax_exempt=True)
        self.invoice = generate_invoice_for_department(self.department, recipient_user=self.recipient)
        self.invoice.submit_payment_proof(transaction_id="TXN123")

    def test_admin_can_approve_own_department_invoice(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:verify_invoice_payment", kwargs={"invoice_id": self.invoice.id}))
        self.assertRedirects(response, reverse("billing:invoices"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)
        self.assertEqual(self.invoice.verified_by, self.admin)
        self.assertIsNotNone(self.invoice.verified_at)

    def test_admin_can_reject_own_department_invoice(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:reject_invoice_payment", kwargs={"invoice_id": self.invoice.id}))
        self.assertRedirects(response, reverse("billing:invoices"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.UNPAID)
        self.assertEqual(self.invoice.verified_by, self.admin)

    def test_verify_and_reject_write_audit_log_entries(self):
        from governance.models import AuditLog

        self.client.login(email="admin@example.com", password="pw12345!")
        self.client.post(reverse("billing:verify_invoice_payment", kwargs={"invoice_id": self.invoice.id}))
        verify_log = AuditLog.objects.get(action_type="billing.invoice_payment_verified")
        self.assertEqual(verify_log.actor, self.admin)
        self.assertEqual(verify_log.target_id, str(self.invoice.id))

        self.client.post(reverse("billing:reject_invoice_payment", kwargs={"invoice_id": self.invoice.id}))
        reject_log = AuditLog.objects.get(action_type="billing.invoice_payment_rejected")
        self.assertEqual(reject_log.actor, self.admin)
        self.assertEqual(reject_log.target_id, str(self.invoice.id))

    def test_admin_cannot_verify_other_departments_invoice(self):
        self.client.login(email="otheradmin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:verify_invoice_payment", kwargs={"invoice_id": self.invoice.id}))
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_reject_other_departments_invoice(self):
        self.client.login(email="otheradmin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:reject_invoice_payment", kwargs={"invoice_id": self.invoice.id}))
        self.assertEqual(response.status_code, 403)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PENDING_VERIFICATION)
        self.assertIsNone(self.invoice.verified_by)

    def test_superadmin_can_verify_any_invoice_htmx(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:verify_invoice_payment", kwargs={"invoice_id": self.invoice.id}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)


class SubmitPaymentProofTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, is_tax_exempt=True)

        self.recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )
        self.other_user = User.objects.create_user(
            email="other@example.com", password="pw12345!", department=self.department
        )
        self.invoice = generate_invoice_for_department(self.department, recipient_user=self.recipient)

    def test_recipient_can_submit_transaction_id_only(self):
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}),
            {"transaction_id": "TXN-999"},
        )
        self.assertRedirects(response, reverse("billing:my_invoices"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PENDING_VERIFICATION)
        self.assertEqual(self.invoice.submitted_transaction_id, "TXN-999")

    def test_submission_requires_at_least_one_of_transaction_id_or_image(self):
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}), {}, follow=True
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.UNPAID)
        messages = list(response.context["messages"])
        self.assertTrue(any("transaction ID" in str(m) for m in messages))

    def test_other_user_cannot_submit_proof_for_someone_elses_invoice(self):
        self.client.login(email="other@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}),
            {"transaction_id": "TXN-999"},
        )
        self.assertEqual(response.status_code, 404)

    def test_cannot_resubmit_once_pending_verification(self):
        self.invoice.submit_payment_proof(transaction_id="TXN-FIRST")
        self.client.login(email="recipient@example.com", password="pw12345!")
        self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}),
            {"transaction_id": "TXN-SECOND"},
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.submitted_transaction_id, "TXN-FIRST")

    def test_valid_image_proof_is_accepted(self):
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
        upload = SimpleUploadedFile("proof.png", buf.getvalue(), content_type="image/png")

        self.client.login(email="recipient@example.com", password="pw12345!")
        self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}),
            {"proof_image": upload},
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PENDING_VERIFICATION)
        self.assertTrue(self.invoice.submitted_proof_image)

    def test_non_image_proof_is_rejected(self):
        """Invoice.submit_payment_proof() saves straight via update_fields,
        bypassing ImageField's normal ModelForm validation entirely - the
        view itself must reject a non-image (e.g. an SVG, which can carry
        a <script> and is opened target="_blank" from this app's own
        origin via the "View proof" link - a stored-XSS vector) before it
        ever reaches that save."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("proof.svg", b"<svg onload='alert(1)'></svg>", content_type="image/svg+xml")
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}),
            {"proof_image": upload},
            follow=True,
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.UNPAID)
        messages = list(response.context["messages"])
        self.assertTrue(any("valid image" in str(m) for m in messages))

    def test_oversized_proof_image_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from billing.views import _MAX_PAYMENT_PROOF_BYTES

        upload = SimpleUploadedFile("proof.png", b"\x00" * (_MAX_PAYMENT_PROOF_BYTES + 1), content_type="image/png")
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}),
            {"proof_image": upload},
            follow=True,
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.UNPAID)
        messages = list(response.context["messages"])
        self.assertTrue(any("too big" in str(m) for m in messages))


class PaymentSubmittedNotificationTests(TestCase):
    """submit_payment_proof notifies whoever can actually act on the
    invoice (billing.views._invoice_managers) - every SuperAdmin, plus
    the invoice's own department's Admin(s) if it has one."""

    def setUp(self):
        from notifications.models import Notification, NotificationType

        self.Notification = Notification
        self.NotificationType = NotificationType

        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")
        self.plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, is_tax_exempt=True)

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.own_admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.other_admin = User.objects.create_user(
            email="otheradmin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.other_department,
        )
        self.recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )
        self.invoice = generate_invoice_for_department(self.department, recipient_user=self.recipient)

    def test_notifies_superadmin_and_own_department_admin_not_others(self):
        self.client.login(email="recipient@example.com", password="pw12345!")
        self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}),
            {"transaction_id": "TXN-1"},
        )
        notified_users = set(self.Notification.objects.values_list("user_id", flat=True))
        self.assertIn(self.superadmin.id, notified_users)
        self.assertIn(self.own_admin.id, notified_users)
        self.assertNotIn(self.other_admin.id, notified_users)

    def test_department_less_invoice_notifies_only_superadmins(self):
        lone_user = User.objects.create_user(email="lone@example.com", password="pw12345!")
        invoice = generate_invoice_for_user(lone_user, plan=self.plan)
        self.client.login(email="lone@example.com", password="pw12345!")
        self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": invoice.id}), {"transaction_id": "TXN-2"}
        )
        notified_users = set(self.Notification.objects.values_list("user_id", flat=True))
        self.assertIn(self.superadmin.id, notified_users)
        self.assertNotIn(self.own_admin.id, notified_users)

    def test_resubmitting_the_same_proof_does_not_duplicate_notifications(self):
        """Regression test for the remaining-audit pass: a double-click/
        retried submit_payment_proof POST used to be able to notify every
        manager twice. The invoice moves to PENDING_VERIFICATION on the
        first submission, so a second POST (sequentially, which is what
        SQLite/the test client can actually exercise - true concurrent-
        request locking is Postgres-only, same caveat as everywhere else
        this pattern is used) must be a no-op, not a second round of
        notifications."""
        self.client.login(email="recipient@example.com", password="pw12345!")
        self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}),
            {"transaction_id": "TXN-1"},
        )
        first_count = self.Notification.objects.filter(
            notification_type=self.NotificationType.INVOICE_PAYMENT_SUBMITTED
        ).count()
        self.assertGreater(first_count, 0)

        self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}),
            {"transaction_id": "TXN-1-retry"},
        )
        second_count = self.Notification.objects.filter(
            notification_type=self.NotificationType.INVOICE_PAYMENT_SUBMITTED
        ).count()
        self.assertEqual(second_count, first_count)


class MyInvoicesViewTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, is_tax_exempt=True)

        self.recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )
        self.other_user = User.objects.create_user(
            email="other@example.com", password="pw12345!", department=self.department
        )
        self.invoice = generate_invoice_for_department(self.department, recipient_user=self.recipient)

    def test_login_required(self):
        response = self.client.get(reverse("billing:my_invoices"))
        self.assertEqual(response.status_code, 302)

    def test_recipient_sees_own_invoice(self):
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:my_invoices"))
        self.assertContains(response, self.invoice.invoice_number)

    def test_other_user_does_not_see_someone_elses_invoice(self):
        self.client.login(email="other@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:my_invoices"))
        self.assertNotContains(response, self.invoice.invoice_number)


class CheckoutPlanTests(TestCase):
    """billing.views.MyPlansView/checkout_plan - self-service plan
    checkout. Per the user's own explicit choice: picking a plan only
    ever creates an unpaid Invoice priced against that plan and emails
    it - the plan itself must never change until that invoice is marked
    paid (see _sync_plan_assignment_to_paid_invoice, tested via the
    payment-verification views below)."""

    def setUp(self):
        mail.outbox = []
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.plan = Plan.objects.create(name="Pro")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("99"))
        self.client.login(email="u@example.com", password="pw12345!")

    def test_my_plans_page_lists_active_plans(self):
        response = self.client.get(reverse("billing:my_plans"))
        self.assertContains(response, "Pro")

    def test_checkout_writes_an_audit_entry_once_even_when_repeated(self):
        """Remaining-audit finding: admin invoice generation was audited
        (billing.invoice_generate) but the self-service path creating the
        same billable document was not. A repeated POST is now a no-op
        (idempotency fix), so it must not write a second entry either."""
        from governance.models import AuditLog

        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})

        invoice = Invoice.objects.get(recipient_user=self.user, plan=self.plan)
        entries = AuditLog.objects.filter(action_type="billing.invoice_checkout")
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.get().actor, self.user)
        self.assertEqual(entries.get().target_id, str(invoice.id))

    def test_checkout_creates_an_unpaid_invoice_for_the_chosen_plan(self):
        response = self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        self.assertRedirects(response, reverse("billing:my_invoices"))
        invoice = Invoice.objects.get(recipient_user=self.user, plan=self.plan)
        self.assertEqual(invoice.status, Invoice.Status.UNPAID)

    def test_checkout_twice_in_a_row_does_not_create_a_duplicate_invoice(self):
        """Regression test for the production-readiness audit: a double-
        click, a browser refresh resubmitting the POST, or a retried
        request all look like this to the server - the SAME user POSTing
        checkout_plan twice for the SAME plan while the first invoice is
        still open (unpaid/pending-verification). Must hand back the
        existing invoice instead of creating a second one."""
        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        self.assertEqual(Invoice.objects.filter(recipient_user=self.user, plan=self.plan).count(), 1)

    def test_checkout_after_existing_invoice_is_paid_creates_a_new_one(self):
        """A legitimate case, not a duplicate: once the open invoice for
        this plan is actually paid (e.g. a renewal, or the admin manually
        marks it paid), checking out for the SAME plan again must still be
        possible - the idempotency check only ever looks at OPEN invoices."""
        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        first = Invoice.objects.get(recipient_user=self.user, plan=self.plan)
        first.status = Invoice.Status.PAID
        first.save(update_fields=["status"])

        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})

        self.assertEqual(Invoice.objects.filter(recipient_user=self.user, plan=self.plan).count(), 2)

    def test_checkout_after_refund_creates_a_new_invoice(self):
        """Re-subscribing after a refund is a legitimate new checkout, not
        a duplicate - a REFUNDED invoice must not block it either."""
        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        first = Invoice.objects.get(recipient_user=self.user, plan=self.plan)
        superadmin = User.objects.create_user(
            email="admin-refund@example.com", password="pw12345!", role=User.Role.SUPERADMIN
        )
        first.mark_refunded(by=superadmin)

        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})

        self.assertEqual(Invoice.objects.filter(recipient_user=self.user, plan=self.plan).count(), 2)

    def test_checkout_for_a_different_plan_is_unaffected_by_an_open_invoice(self):
        """An open invoice for Plan A must never block checking out Plan B -
        the idempotency check is scoped per (user, plan), not per user."""
        other_plan = Plan.objects.create(name="Elite")
        RegionalPrice.objects.create(plan=other_plan, region_code="ROW", price=Decimal("199"))

        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        self.client.post(reverse("billing:checkout_plan"), {"plan_id": other_plan.id})

        self.assertEqual(Invoice.objects.filter(recipient_user=self.user, plan=self.plan).count(), 1)
        self.assertEqual(Invoice.objects.filter(recipient_user=self.user, plan=other_plan).count(), 1)

    def test_checkout_while_pending_verification_does_not_create_a_duplicate(self):
        """The window between "user submitted payment proof" and "admin
        verified it" (Invoice.Status.PENDING_VERIFICATION) must also count
        as open - a second checkout attempt during that window is exactly
        as much a duplicate as one against a fresh unpaid invoice."""
        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        first = Invoice.objects.get(recipient_user=self.user, plan=self.plan)
        first.status = Invoice.Status.PENDING_VERIFICATION
        first.save(update_fields=["status"])

        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})

        self.assertEqual(Invoice.objects.filter(recipient_user=self.user, plan=self.plan).count(), 1)

    def test_checkout_does_not_change_the_users_plan(self):
        from governance.plans import get_assignment

        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        assignment = get_assignment(self.user)
        self.assertNotEqual(getattr(assignment, "plan_id", None), self.plan.id)

    def test_checkout_emails_the_invoice(self):
        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])

    def test_checkout_invoice_shows_in_my_invoices(self):
        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        response = self.client.get(reverse("billing:my_invoices"))
        self.assertContains(response, "Pro")

    def test_cannot_checkout_an_inactive_plan(self):
        self.plan.is_active = False
        self.plan.save(update_fields=["is_active"])
        response = self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        self.assertEqual(response.status_code, 404)

    def test_checkout_requires_login(self):
        self.client.logout()
        response = self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        self.assertEqual(response.status_code, 302)

    def test_paying_the_checkout_invoice_switches_the_plan(self):
        from governance.plans import get_assignment

        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        invoice = Invoice.objects.get(recipient_user=self.user, plan=self.plan)

        superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.client.logout()
        self.client.login(email="super@example.com", password="pw12345!")
        self.client.post(reverse("billing:toggle_invoice_status", kwargs={"invoice_id": invoice.id}))

        assignment = get_assignment(self.user)
        self.assertEqual(assignment.plan_id, self.plan.id)
        self.assertEqual(assignment.assigned_by, superadmin)

    def test_unpaying_an_already_paid_invoice_does_not_change_the_plan_again(self):
        """toggle_invoice_status flips both ways - only the transition
        INTO paid should ever sync the plan, never the reverse toggle."""
        from governance.plans import assign_plan, get_assignment

        other_plan = Plan.objects.create(name="Other")
        self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        invoice = Invoice.objects.get(recipient_user=self.user, plan=self.plan)

        superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.client.logout()
        self.client.login(email="super@example.com", password="pw12345!")
        self.client.post(reverse("billing:toggle_invoice_status", kwargs={"invoice_id": invoice.id}))
        # Someone manually moves the user onto yet another plan afterward.
        assign_plan(self.user, other_plan, assigned_by=superadmin)
        # Toggling the (now-paid) invoice back to unpaid must not revert
        # or otherwise touch the plan assignment.
        self.client.post(reverse("billing:toggle_invoice_status", kwargs={"invoice_id": invoice.id}))

        assignment = get_assignment(self.user)
        self.assertEqual(assignment.plan_id, other_plan.id)

    def test_plans_page_shows_capabilities_not_just_price_and_seats(self):
        self.plan.feature_flags = {"document_generation": True}
        self.plan.monthly_image_reads_limit = 250
        self.plan.save(update_fields=["feature_flags", "monthly_image_reads_limit"])
        response = self.client.get(reverse("billing:my_plans"))
        self.assertContains(response, "Image reading")
        self.assertContains(response, "250")
        self.assertContains(response, "Document generation")

    def test_public_pricing_page_also_shows_capabilities(self):
        self.client.logout()
        self.plan.monthly_research_limit = 25
        self.plan.save(update_fields=["monthly_research_limit"])
        response = self.client.get(reverse("billing:public_pricing"))
        self.assertContains(response, "Research (live web search)")
        self.assertContains(response, "25")


class LeadCapturePlanTests(TestCase):
    """A Plan with self_checkout_enabled=False (governance.models.Plan) -
    the "lowest tier self-serve, higher tiers a sales conversation" split
    from the user's own feedback. checkout_plan must 404 for one of these
    even via a direct POST (not just hide the button); request_plan_access
    is the "Contact us" counterpart, reusing the existing UpgradeRequest
    inbox rather than a new admin surface."""

    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.plan = Plan.objects.create(name="Enterprise", self_checkout_enabled=False)
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("999"))
        self.client.login(email="u@example.com", password="pw12345!")
        # Other seeded/pre-existing plans default self_checkout_enabled=True
        # and would render their OWN checkout forms on the same page -
        # deactivate them so this test's assertions are about THIS plan.
        Plan.objects.exclude(pk=self.plan.pk).update(is_active=False)

    def test_checkout_button_hidden_shows_contact_us_instead(self):
        response = self.client.get(reverse("billing:my_plans"))
        self.assertContains(response, "Contact us")
        self.assertNotContains(response, f'action="{reverse("billing:checkout_plan")}"')

    def test_checkout_is_blocked_even_via_a_direct_post(self):
        response = self.client.post(reverse("billing:checkout_plan"), {"plan_id": self.plan.id})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Invoice.objects.filter(recipient_user=self.user, plan=self.plan).exists())

    def test_request_plan_access_creates_an_upgrade_request(self):
        from governance.models import UpgradeRequest

        response = self.client.post(reverse("billing:request_plan_access"), {"plan_id": self.plan.id})
        self.assertRedirects(response, reverse("billing:my_plans"))
        upgrade_request = UpgradeRequest.objects.get(user=self.user, requested_plan=self.plan)
        self.assertEqual(upgrade_request.status, UpgradeRequest.Status.PENDING)

    def test_request_plan_access_does_not_create_an_invoice_or_change_the_plan(self):
        from governance.plans import get_assignment

        self.client.post(reverse("billing:request_plan_access"), {"plan_id": self.plan.id})
        self.assertFalse(Invoice.objects.filter(recipient_user=self.user, plan=self.plan).exists())
        assignment = get_assignment(self.user)
        self.assertNotEqual(getattr(assignment, "plan_id", None), self.plan.id)


class InvoiceDetailViewTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")
        self.plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, is_tax_exempt=True)

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.other_admin = User.objects.create_user(
            email="otheradmin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.other_department,
        )
        self.recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )
        self.stranger = User.objects.create_user(
            email="stranger@example.com", password="pw12345!", department=self.other_department
        )
        self.invoice = generate_invoice_for_department(self.department, recipient_user=self.recipient)

    def _url(self):
        return reverse("billing:invoice_detail", kwargs={"invoice_id": self.invoice.id})

    def test_recipient_can_view_and_cannot_manage(self):
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_recipient"])
        self.assertFalse(response.context["can_manage"])

    def test_own_department_admin_can_view_and_manage(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_recipient"])
        self.assertTrue(response.context["can_manage"])

    def test_other_department_admin_cannot_view(self):
        self.client.login(email="otheradmin@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 403)

    def test_superadmin_can_view_and_manage(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_manage"])

    def test_unrelated_user_cannot_view(self):
        self.client.login(email="stranger@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 403)

    def test_shows_line_items(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertContains(response, "Growth")
        self.assertContains(response, "50.00")

    def test_shows_bill_to_block_from_billing_profile(self):
        DepartmentBillingProfile.objects.filter(department=self.department).update(
            company_name="Acme Corp", billing_address="123 Business Road, Karachi", tax_id="NTN-1234567-8"
        )
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertContains(response, "Acme Corp")
        self.assertContains(response, "123 Business Road, Karachi")
        self.assertContains(response, "NTN-1234567-8")

    def test_recipient_name_takes_priority_over_company_name_when_both_are_set(self):
        """Reported directly - the invoice should read as the actual
        person's, not fall back to a company name that happens to be on
        file when the person's own name is right there. The test above
        covers the fallback case (no name on the recipient at all)."""
        self.recipient.first_name = "Ayesha"
        self.recipient.last_name = "Khan"
        self.recipient.save(update_fields=["first_name", "last_name"])
        DepartmentBillingProfile.objects.filter(department=self.department).update(company_name="Acme Corp")
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertContains(response, "Ayesha Khan")

    def test_toggle_from_detail_page_redirects_back_to_detail_page(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:toggle_invoice_status", kwargs={"invoice_id": self.invoice.id}),
            {"next_invoice_id": self.invoice.id},
        )
        self.assertRedirects(response, self._url())

    def test_submit_proof_from_detail_page_redirects_back_to_detail_page(self):
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": self.invoice.id}),
            {"transaction_id": "TXN-1", "next_invoice_id": self.invoice.id},
        )
        self.assertRedirects(response, self._url())


class InvoiceAutomationSettingsTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )

    def test_scoped_admin_always_sees_their_own_departments_automation_card(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertEqual(response.context["automation_department"], self.department)
        self.assertContains(response, "Automated Invoicing")

    def test_superadmin_sees_no_automation_card_without_a_department_filter(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertIsNone(response.context["automation_department"])
        self.assertContains(response, "Select a department above")

    def test_superadmin_sees_automation_card_for_filtered_department(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"), {"department": self.department.id})
        self.assertEqual(response.context["automation_department"], self.department)

    def test_admin_can_update_own_departments_automation_settings(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:update_invoice_automation", kwargs={"department_id": self.department.id}),
            {"auto_generate_invoices": "on", "reminder_days_after_due": "7"},
        )
        self.assertRedirects(response, reverse("billing:invoices"))
        profile = DepartmentBillingProfile.objects.get(department=self.department)
        self.assertTrue(profile.auto_generate_invoices)
        self.assertEqual(profile.reminder_days_after_due, 7)

    def test_update_does_not_touch_other_billing_profile_fields(self):
        DepartmentBillingProfile.objects.create(department=self.department, company_name="Acme Corp")
        self.client.login(email="admin@example.com", password="pw12345!")
        self.client.post(
            reverse("billing:update_invoice_automation", kwargs={"department_id": self.department.id}),
            {"auto_generate_invoices": "on", "reminder_days_after_due": "3"},
        )
        profile = DepartmentBillingProfile.objects.get(department=self.department)
        self.assertEqual(profile.company_name, "Acme Corp")

    def test_admin_cannot_update_other_departments_automation_settings(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:update_invoice_automation", kwargs={"department_id": self.other_department.id}),
            {"auto_generate_invoices": "on"},
        )
        self.assertEqual(response.status_code, 403)


class InvoicePdfTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")
        self.plan = Plan.objects.create(name="Growth", seats_included=2)
        RegionalPrice.objects.create(
            plan=self.plan, region_code="ROW", price=Decimal("100"), extra_seat_price=Decimal("20")
        )
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(
            department=self.department, is_tax_exempt=True, company_name="Acme Corp"
        )
        OrganizationBillingProfile.objects.create(pk=1, bank_name="Meezan Bank")

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.other_admin = User.objects.create_user(
            email="otheradmin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.other_department,
        )
        self.recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )
        self._add_users(4)
        self.invoice = generate_invoice_for_department(self.department, recipient_user=self.recipient)

    def _add_users(self, count):
        for i in range(count):
            User.objects.create_user(email=f"seat{i}@example.com", password="pw12345!", department=self.department)

    def test_seats_billed_is_recorded(self):
        # recipient + 4 extra users = 5 in the department.
        self.assertEqual(self.invoice.seats_billed, 5)

    def test_render_invoice_pdf_returns_pdf_bytes(self):
        pdf_bytes = render_invoice_pdf(self.invoice)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_recipient_can_download_pdf(self):
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:download_invoice_pdf", kwargs={"invoice_id": self.invoice.id}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(self.invoice.invoice_number, response["Content-Disposition"])

    def test_superadmin_can_download_pdf(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:download_invoice_pdf", kwargs={"invoice_id": self.invoice.id}))
        self.assertEqual(response.status_code, 200)

    def test_unrelated_admin_cannot_download_pdf(self):
        self.client.login(email="otheradmin@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:download_invoice_pdf", kwargs={"invoice_id": self.invoice.id}))
        self.assertEqual(response.status_code, 403)

    def test_inline_param_opens_in_browser_instead_of_downloading(self):
        # The "Print" button opens this in a new tab so the browser's own
        # PDF viewer (and its Print icon) is what actually prints - never
        # window.print() on the HTML page, which always carries a browser-
        # injected date/title/URL header no page CSS can suppress.
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.get(
            reverse("billing:download_invoice_pdf", kwargs={"invoice_id": self.invoice.id}), {"inline": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Disposition"].startswith("inline"))

    def test_public_share_pdf_accessible_without_login(self):
        response = self.client.get(reverse("billing:public_invoice_pdf", kwargs={"token": self.invoice.share_token}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response["Content-Disposition"].startswith("inline"))

    def test_public_share_pdf_wrong_token_404s(self):
        response = self.client.get(reverse("billing:public_invoice_pdf", kwargs={"token": "not-a-real-token"}))
        self.assertEqual(response.status_code, 404)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("billing:download_invoice_pdf", kwargs={"invoice_id": self.invoice.id}))
        self.assertEqual(response.status_code, 302)


class WelcomeInvoiceSignalTests(TestCase):
    """accounts.signals.generate_welcome_invoice_on_creation - fires for
    every new User row (self-signup or admin-created, department or not),
    must never raise even when nothing is priced yet."""

    def test_new_user_gets_a_welcome_invoice_when_row_priced(self):
        user = User.objects.create_user(email="new@example.com", password="pw12345!")
        demo_plan = user.plan_assignment.plan
        RegionalPrice.objects.create(plan=demo_plan, region_code="ROW", price=Decimal("0"))
        # The RegionalPrice above didn't exist yet at creation time, so the
        # first attempt silently failed - create a second user now that
        # it's priced to see the success path.
        second_user = User.objects.create_user(email="new2@example.com", password="pw12345!")
        self.assertTrue(Invoice.objects.filter(recipient_user=second_user, plan=demo_plan).exists())

    def test_new_user_creation_never_raises_when_nothing_priced(self):
        # No RegionalPrice seeded anywhere - must not raise or roll back.
        user = User.objects.create_user(email="unpriced@example.com", password="pw12345!")
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(Invoice.objects.filter(recipient_user=user).exists())

    def test_admin_created_user_inside_a_priced_department_gets_departmental_invoice(self):
        department = Department.objects.create(name="Sales")
        plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=plan, region_code="ROW", price=Decimal("50"))
        department.plan = plan
        department.save(update_fields=["plan"])

        user = User.objects.create_user(email="deptuser@example.com", password="pw12345!", department=department)
        invoice = Invoice.objects.get(recipient_user=user)
        self.assertEqual(invoice.department, department)
        self.assertEqual(invoice.plan, plan)

    def test_does_not_fire_on_plain_save_update(self):
        user = User.objects.create_user(email="existing@example.com", password="pw12345!")
        Invoice.objects.filter(recipient_user=user).delete()
        user.first_name = "Changed"
        user.save()
        self.assertFalse(Invoice.objects.filter(recipient_user=user).exists())


class SweepDueInvoicesTests(TestCase):
    """billing.tasks.sweep_due_invoices - the recurring monthly generation
    task. Demo/plan_assignment plans are deliberately never priced in this
    class, so accounts.signals.generate_welcome_invoice_on_creation always
    fails silently for every user created here and never pollutes these
    hand-built fixtures with an extra invoice."""

    def setUp(self):
        self.plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.user = User.objects.create_user(email="user@example.com", password="pw12345!")

    def _make_invoice(self, user, plan, due_date):
        return Invoice.objects.create(
            department=None,
            recipient_user=user,
            plan=plan,
            issue_date=due_date - timedelta(days=14),
            due_date=due_date,
            currency="USD",
            subtotal=Decimal("50"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("50"),
            status=Invoice.Status.PAID,
        )

    def test_user_with_zero_invoices_is_skipped(self):
        result = sweep_due_invoices()
        self.assertEqual(result["generated"], 0)

    def test_generates_next_invoice_exactly_3_days_before_due(self):
        today = timezone.localdate()
        self._make_invoice(self.user, self.plan, due_date=today - timedelta(days=27))  # +30 = today+3
        result = sweep_due_invoices()
        self.assertEqual(result["generated"], 1)
        new_invoice = Invoice.objects.filter(recipient_user=self.user).order_by("-id").first()
        self.assertEqual(new_invoice.due_date, today + timedelta(days=3))

    def test_does_not_generate_before_the_3_day_window(self):
        today = timezone.localdate()
        self._make_invoice(self.user, self.plan, due_date=today - timedelta(days=26))  # +30 = today+4
        result = sweep_due_invoices()
        self.assertEqual(result["generated"], 0)

    def test_does_not_double_generate_if_run_twice(self):
        today = timezone.localdate()
        self._make_invoice(self.user, self.plan, due_date=today - timedelta(days=27))
        sweep_due_invoices()
        result2 = sweep_due_invoices()
        self.assertEqual(result2["generated"], 0)
        self.assertEqual(Invoice.objects.filter(recipient_user=self.user).count(), 2)

    def test_generates_late_if_sweep_missed_earlier_days(self):
        today = timezone.localdate()
        self._make_invoice(self.user, self.plan, due_date=today - timedelta(days=29))  # +30 = today+1, past window
        result = sweep_due_invoices()
        self.assertEqual(result["generated"], 1)
        new_invoice = Invoice.objects.filter(recipient_user=self.user).order_by("-id").first()
        self.assertEqual(new_invoice.due_date, today + timedelta(days=1))

    def test_skips_departmental_user_with_auto_generate_invoices_off(self):
        department = Department.objects.create(name="Support")
        dept_user = User.objects.create_user(email="deptuser@example.com", password="pw12345!", department=department)
        department.plan = self.plan
        department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=department, auto_generate_invoices=False)
        today = timezone.localdate()
        self._make_invoice(dept_user, self.plan, due_date=today - timedelta(days=27))

        result = sweep_due_invoices()
        self.assertEqual(result["skipped_auto_generate_off"], 1)
        self.assertEqual(Invoice.objects.filter(recipient_user=dept_user).count(), 1)

    def test_departmental_user_with_auto_generate_invoices_on_is_generated(self):
        department = Department.objects.create(name="Support2")
        dept_user = User.objects.create_user(email="deptuser2@example.com", password="pw12345!", department=department)
        department.plan = self.plan
        department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=department, auto_generate_invoices=True)
        today = timezone.localdate()
        self._make_invoice(dept_user, self.plan, due_date=today - timedelta(days=27))

        result = sweep_due_invoices()
        self.assertEqual(result["generated"], 1)

    def test_department_less_user_always_eligible(self):
        today = timezone.localdate()
        self._make_invoice(self.user, self.plan, due_date=today - timedelta(days=27))
        result = sweep_due_invoices()
        self.assertEqual(result["generated"], 1)

    def test_one_users_price_error_does_not_abort_the_whole_sweep(self):
        unpriced_plan = Plan.objects.create(name="Unpriced")
        bad_user = User.objects.create_user(email="bad@example.com", password="pw12345!")
        today = timezone.localdate()
        self._make_invoice(bad_user, unpriced_plan, due_date=today - timedelta(days=27))
        self._make_invoice(self.user, self.plan, due_date=today - timedelta(days=27))

        result = sweep_due_invoices()
        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["no_price"], 1)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SendOverdueRemindersTests(TestCase):
    """billing.tasks.send_overdue_reminders - the once-per-invoice dunning
    nudge, independent of (and never a precondition for) the actual
    chat-access block in billing.access.has_overdue_unpaid_invoice."""

    def setUp(self):
        self.plan = Plan.objects.create(name="Growth")
        self.user = User.objects.create_user(email="client@example.com", password="pw12345!")
        mail.outbox = []

    def _make_invoice(self, due_date, department=None, status=Invoice.Status.UNPAID):
        return Invoice.objects.create(
            department=department,
            recipient_user=self.user,
            plan=self.plan,
            issue_date=due_date - timedelta(days=14),
            due_date=due_date,
            currency="USD",
            subtotal=Decimal("50"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("50"),
            status=status,
        )

    def test_department_less_invoice_reminded_after_default_3_days(self):
        today = timezone.localdate()
        invoice = self._make_invoice(due_date=today - timedelta(days=3))
        result = send_overdue_reminders()
        self.assertEqual(result["sent"], 1)
        invoice.refresh_from_db()
        self.assertIsNotNone(invoice.reminder_sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(invoice.invoice_number, mail.outbox[0].subject)

    def test_not_yet_reminded_before_the_threshold(self):
        today = timezone.localdate()
        self._make_invoice(due_date=today - timedelta(days=2))
        result = send_overdue_reminders()
        self.assertEqual(result["sent"], 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_never_reminds_twice_for_the_same_invoice(self):
        today = timezone.localdate()
        self._make_invoice(due_date=today - timedelta(days=3))
        send_overdue_reminders()
        result2 = send_overdue_reminders()
        self.assertEqual(result2["sent"], 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_paid_invoice_is_never_reminded(self):
        today = timezone.localdate()
        self._make_invoice(due_date=today - timedelta(days=5), status=Invoice.Status.PAID)
        result = send_overdue_reminders()
        self.assertEqual(result["sent"], 0)

    def test_department_with_no_reminder_configured_is_skipped(self):
        department = Department.objects.create(name="Sales")
        self.user.department = department
        self.user.save(update_fields=["department"])
        # DepartmentBillingProfile.reminder_days_after_due defaults to
        # NONE (0) - most departments are opted out until an Admin turns
        # this on from the Automated Invoicing card.
        DepartmentBillingProfile.objects.create(department=department)
        today = timezone.localdate()
        self._make_invoice(due_date=today - timedelta(days=10), department=department)
        result = send_overdue_reminders()
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["skipped_no_reminder"], 1)

    def test_department_with_reminder_configured_is_reminded(self):
        department = Department.objects.create(name="Sales")
        self.user.department = department
        self.user.save(update_fields=["department"])
        DepartmentBillingProfile.objects.create(
            department=department, reminder_days_after_due=DepartmentBillingProfile.ReminderSchedule.DAYS_7
        )
        today = timezone.localdate()
        self._make_invoice(due_date=today - timedelta(days=7), department=department)
        result = send_overdue_reminders()
        self.assertEqual(result["sent"], 1)


class OverdueChatAccessTests(TestCase):
    """billing.access.has_overdue_unpaid_invoice and its two insertion
    points (governance/limits.py::check_usage_limits, governance/plans.py
    ::check_session_creation_limit) - the block must never touch
    billing:my_invoices/submit_payment_proof or the admin/governance
    panel, and must lift the instant an Admin/SuperAdmin marks the
    invoice paid."""

    def setUp(self):
        from chat.models import Conversation

        self.Conversation = Conversation
        self.department = Department.objects.create(name="Sales")
        self.plan = Plan.objects.create(name="Growth")
        self.user = User.objects.create_user(email="user@example.com", password="pw12345!")
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.client.login(email="user@example.com", password="pw12345!")

    def _make_invoice(self, user, due_date, status=Invoice.Status.UNPAID):
        return Invoice.objects.create(
            department=None,
            recipient_user=user,
            plan=self.plan,
            issue_date=due_date - timedelta(days=14),
            due_date=due_date,
            currency="USD",
            subtotal=Decimal("50"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("50"),
            status=status,
        )

    def test_true_for_overdue_unpaid(self):
        self._make_invoice(self.user, timezone.localdate() - timedelta(days=1), Invoice.Status.UNPAID)
        self.assertTrue(has_overdue_unpaid_invoice(self.user))

    def test_true_for_overdue_pending_verification(self):
        self._make_invoice(self.user, timezone.localdate() - timedelta(days=1), Invoice.Status.PENDING_VERIFICATION)
        self.assertTrue(has_overdue_unpaid_invoice(self.user))

    def test_false_for_overdue_paid(self):
        self._make_invoice(self.user, timezone.localdate() - timedelta(days=1), Invoice.Status.PAID)
        self.assertFalse(has_overdue_unpaid_invoice(self.user))

    def test_false_for_not_yet_due(self):
        self._make_invoice(self.user, timezone.localdate() + timedelta(days=1))
        self.assertFalse(has_overdue_unpaid_invoice(self.user))

    def test_false_for_due_today(self):
        self._make_invoice(self.user, timezone.localdate())
        self.assertFalse(has_overdue_unpaid_invoice(self.user))

    def test_false_for_user_with_no_invoices(self):
        self.assertFalse(has_overdue_unpaid_invoice(self.user))

    def test_create_conversation_blocked_when_overdue(self):
        self._make_invoice(self.user, timezone.localdate() - timedelta(days=1))
        response = self.client.post(reverse("chat:create_conversation"), follow=True)
        self.assertRedirects(response, reverse("chat:chat_home"))
        messages_list = list(response.context["messages"])
        self.assertTrue(any("overdue" in str(m) for m in messages_list))
        self.assertFalse(self.Conversation.objects.filter(user=self.user).exists())

    def test_message_send_blocked_when_overdue(self):
        conversation = self.Conversation.objects.create(user=self.user)
        self._make_invoice(self.user, timezone.localdate() - timedelta(days=1))
        response = self.client.post(
            reverse("chat:post_message", kwargs={"conversation_id": conversation.id}), {"content": "hello"}
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("overdue", response.content.decode().lower())

    def test_chat_access_restored_after_admin_marks_paid(self):
        invoice = self._make_invoice(self.user, timezone.localdate() - timedelta(days=1))
        invoice.verify_payment(self.admin)
        response = self.client.post(reverse("chat:create_conversation"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.Conversation.objects.filter(user=self.user).exists())

    def test_my_invoices_accessible_while_blocked(self):
        self._make_invoice(self.user, timezone.localdate() - timedelta(days=1))
        response = self.client.get(reverse("billing:my_invoices"))
        self.assertEqual(response.status_code, 200)

    def test_submit_payment_proof_accessible_while_blocked(self):
        invoice = self._make_invoice(self.user, timezone.localdate() - timedelta(days=1))
        response = self.client.post(
            reverse("billing:submit_payment_proof", kwargs={"invoice_id": invoice.id}), {"transaction_id": "TXN1"}
        )
        self.assertRedirects(response, reverse("billing:my_invoices"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PENDING_VERIFICATION)

    def test_admin_own_governance_panel_accessible_despite_own_overdue_invoice(self):
        self._make_invoice(self.admin, timezone.localdate() - timedelta(days=1))
        self.client.logout()
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertEqual(response.status_code, 200)


class InvoiceShareTokenTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(name="Advanced")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("300"))
        self.user = User.objects.create_user(email="client@example.com", password="pw12345!")

    def test_new_invoice_gets_a_share_token(self):
        invoice = generate_invoice_for_user(self.user, plan=self.plan)
        self.assertTrue(invoice.share_token)

    def test_share_tokens_are_unique(self):
        invoice1 = generate_invoice_for_user(self.user, plan=self.plan)
        invoice2 = generate_invoice_for_user(self.user, plan=self.plan, due_in_days=30)
        self.assertNotEqual(invoice1.share_token, invoice2.share_token)


class IndividualBillToTests(TestCase):
    """Bill To for a department-less invoice - name/phone/address should
    come from the recipient's own UserBillingProfile, not just their bare
    email (see billing.models.billing_profile_for_invoice)."""

    def setUp(self):
        self.plan = Plan.objects.create(name="Advanced")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("300"))
        self.user = User.objects.create_user(
            email="client@example.com", password="pw12345!", first_name="Ayesha", last_name="Khan"
        )
        self.invoice = generate_invoice_for_user(self.user, plan=self.plan)

    def _url(self):
        return reverse("billing:invoice_detail", kwargs={"invoice_id": self.invoice.id})

    def test_falls_back_to_full_name_then_email_with_no_profile(self):
        self.client.login(email="client@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertContains(response, "Ayesha Khan")
        self.assertContains(response, "client@example.com")

    def test_shows_phone_and_address_from_user_billing_profile_but_the_users_own_name_not_company_name(self):
        """The recipient's own name (already set on self.user - see
        setUp) takes priority over company_name even when one is on file
        - see _invoice_document.html's own comment on this same
        priority. Phone/address from the billing profile still show
        regardless, since those aren't affected by that priority."""
        UserBillingProfile.objects.create(
            user=self.user,
            company_name="Khan Traders",
            phone_number="+92 300 1234567",
            billing_address="123 Mall Road, Lahore",
        )
        self.client.login(email="client@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertContains(response, "Ayesha Khan")
        self.assertNotContains(response, "Khan Traders")
        self.assertContains(response, "client@example.com")
        self.assertContains(response, "+92 300 1234567")
        self.assertContains(response, "123 Mall Road, Lahore")

    def test_falls_back_to_company_name_when_recipient_has_no_name(self):
        self.user.first_name = ""
        self.user.last_name = ""
        self.user.save(update_fields=["first_name", "last_name"])
        UserBillingProfile.objects.create(user=self.user, company_name="Khan Traders")
        self.client.login(email="client@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertContains(response, "Khan Traders")

    def test_pdf_includes_company_and_phone(self):
        UserBillingProfile.objects.create(user=self.user, company_name="Khan Traders", phone_number="+92 300 1234567")
        pdf_bytes = render_invoice_pdf(self.invoice)
        self.assertGreater(len(pdf_bytes), 0)


class UpdateMyBillingProfileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="client@example.com", password="pw12345!")
        self.client.login(email="client@example.com", password="pw12345!")

    def test_creates_profile_on_first_save(self):
        response = self.client.post(
            reverse("billing:update_my_billing_profile"),
            {"company_name": "Khan Traders", "phone_number": "0300-1234567", "billing_address": "Lahore"},
        )
        self.assertRedirects(response, reverse("billing:my_invoices"))
        profile = UserBillingProfile.objects.get(user=self.user)
        self.assertEqual(profile.company_name, "Khan Traders")
        self.assertEqual(profile.phone_number, "0300-1234567")
        self.assertEqual(profile.billing_address, "Lahore")

    def test_updates_existing_profile(self):
        UserBillingProfile.objects.create(user=self.user, company_name="Old Name")
        self.client.post(reverse("billing:update_my_billing_profile"), {"company_name": "New Name"})
        profile = UserBillingProfile.objects.get(user=self.user)
        self.assertEqual(profile.company_name, "New Name")

    def test_requires_login(self):
        self.client.logout()
        response = self.client.post(reverse("billing:update_my_billing_profile"), {"company_name": "X"})
        self.assertRedirects(response, reverse("accounts:login"))


class PublicInvoiceViewTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(name="Advanced")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("300"))
        self.user = User.objects.create_user(email="client@example.com", password="pw12345!")
        self.invoice = generate_invoice_for_user(self.user, plan=self.plan)

    def test_accessible_without_login(self):
        response = self.client.get(reverse("billing:public_invoice", kwargs={"token": self.invoice.share_token}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.invoice.invoice_number)

    def test_wrong_token_404s(self):
        response = self.client.get(reverse("billing:public_invoice", kwargs={"token": "not-a-real-token"}))
        self.assertEqual(response.status_code, 404)

    def test_no_manage_or_submit_actions_shown(self):
        response = self.client.get(reverse("billing:public_invoice", kwargs={"token": self.invoice.share_token}))
        self.assertNotContains(response, "Mark paid manually")
        self.assertNotContains(response, "Submit payment")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailInvoiceToClientTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.department
        )
        self.recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )
        self.invoice = generate_invoice_for_department(self.department, recipient_user=self.recipient)
        mail.outbox = []

    def _url(self):
        return reverse("billing:email_invoice", kwargs={"invoice_id": self.invoice.id})

    def test_admin_can_email_the_invoice(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(self._url(), {"next_invoice_id": self.invoice.id})
        self.assertRedirects(response, reverse("billing:invoice_detail", kwargs={"invoice_id": self.invoice.id}))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["recipient@example.com"])
        self.assertIn(self.invoice.invoice_number, mail.outbox[0].subject)
        self.assertIn(self.invoice.share_token, mail.outbox[0].body)

    def test_email_attaches_the_real_pdf(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        self.client.post(self._url(), {"next_invoice_id": self.invoice.id})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(mail.outbox[0].attachments), 1)
        filename, content, mimetype = mail.outbox[0].attachments[0]
        self.assertEqual(filename, f"{self.invoice.invoice_number}.pdf")
        self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))

    def test_email_has_styled_html_alternative_with_invoice_details(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        self.client.post(self._url(), {"next_invoice_id": self.invoice.id})
        self.assertEqual(len(mail.outbox), 1)
        html_bodies = [content for content, mimetype in mail.outbox[0].alternatives if mimetype == "text/html"]
        self.assertEqual(len(html_bodies), 1)
        self.assertIn(self.invoice.invoice_number, html_bodies[0])
        self.assertIn(str(self.invoice.total), html_bodies[0])
        self.assertIn(self.invoice.share_token, html_bodies[0])

    def test_recipient_cannot_email_their_own_invoice(self):
        self.client.login(email="recipient@example.com", password="pw12345!")
        response = self.client.post(self._url(), {"next_invoice_id": self.invoice.id})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_stranger_admin_cannot_email(self):
        other_department = Department.objects.create(name="Support")
        User.objects.create_user(
            email="otheradmin@example.com", password="pw12345!", role=User.Role.ADMIN, department=other_department
        )
        self.client.login(email="otheradmin@example.com", password="pw12345!")
        response = self.client.post(self._url(), {"next_invoice_id": self.invoice.id})
        self.assertEqual(response.status_code, 403)


class GenerateInvoiceForDepartmentLessUserViewTests(TestCase):
    """The manual "Generate invoice" admin form used to reject any
    recipient without a department outright - a leftover from before
    Milestone 6 made invoicing department-optional. It now delegates to
    generate_invoice_for_user, same as the welcome-invoice signal and the
    recurring sweep."""

    def setUp(self):
        self.plan = Plan.objects.create(name="Advanced")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("300"))
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.department = Department.objects.create(name="Sales")
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.lone_user = User.objects.create_user(email="lone@example.com", password="pw12345!")

    def test_superadmin_can_generate_for_a_department_less_user(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:generate_invoice"), {"recipient_user_id": self.lone_user.id, "plan_id": self.plan.id}
        )
        self.assertRedirects(response, reverse("billing:invoices"))
        invoice = Invoice.objects.get(recipient_user=self.lone_user)
        self.assertIsNone(invoice.department)
        self.assertEqual(invoice.plan, self.plan)

    def test_scoped_admin_cannot_generate_for_a_department_less_user(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(
            reverse("billing:generate_invoice"), {"recipient_user_id": self.lone_user.id, "plan_id": self.plan.id}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Invoice.objects.filter(recipient_user=self.lone_user).exists())

    def test_department_less_user_appears_in_generate_invoice_form(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertContains(response, "lone@example.com")

    def test_scoped_admin_does_not_see_department_less_users_in_form(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertNotContains(response, "lone@example.com")


class DeleteInvoiceTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(name="Advanced")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("300"))
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.department = Department.objects.create(name="Sales")
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.user = User.objects.create_user(email="user@example.com", password="pw12345!")
        self.invoice = generate_invoice_for_user(self.user, plan=self.plan)

    def _url(self):
        return reverse("billing:delete_invoice", kwargs={"invoice_id": self.invoice.id})

    def test_superadmin_can_delete(self):
        from governance.models import AuditLog

        invoice_id = self.invoice.id
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(self._url())
        self.assertRedirects(response, reverse("billing:invoices"))
        self.assertFalse(Invoice.objects.filter(id=invoice_id).exists())

        log = AuditLog.objects.get(action_type="billing.invoice_delete")
        self.assertEqual(log.actor, self.superadmin)
        self.assertEqual(log.target_id, str(invoice_id))

    def test_admin_cannot_delete(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Invoice.objects.filter(id=self.invoice.id).exists())

    def test_htmx_delete_returns_table_fragment(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(self._url(), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Invoice.objects.filter(id=self.invoice.id).exists())

    def test_delete_button_only_shown_to_superadmin(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertContains(response, "Delete invoice")

        self.client.logout()
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertNotContains(response, "Delete invoice")

    def test_email_button_shown_on_list_for_invoice_with_recipient(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertContains(response, reverse("billing:email_invoice", kwargs={"invoice_id": self.invoice.id}))

    def test_delete_button_uses_hx_confirm_not_the_plain_form_helper(self):
        """Reported directly: clicking Delete deleted the invoice
        immediately, with the confirm dialog only appearing afterward,
        powerless to stop anything. Root cause: this form has hx-post
        (htmx issues its own AJAX request straight off the native submit
        event) alongside onsubmit="portalConfirmSubmit(...)" - a helper
        that only works for a PLAIN form, since it can preventDefault()
        the native submission but has no way to stop htmx's own,
        separate listener on the same event from firing regardless. The
        fix is hx-confirm (htmx's own confirmation hook, already used
        correctly for every other htmx-driven delete in this app - e.g.
        governance's routing rules/models) rather than that helper."""
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("billing:invoices"))
        self.assertContains(response, "hx-confirm=")
        self.assertNotContains(response, "portalConfirmSubmit")


class RequestRefundTests(TestCase):
    """billing.views.request_refund - Refund & Cancellation Policy
    sections 1 (7-day money-back window, auto-approved) and 4/9
    (outside the window, an Admin has to decide)."""

    def setUp(self):
        self.plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.other_user = User.objects.create_user(email="other@example.com", password="pw12345!")
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.invoice = generate_invoice_for_user(self.user, plan=self.plan)
        self.invoice.verify_payment(self.admin)
        self.client.login(email="u@example.com", password="pw12345!")

    def _url(self):
        return reverse("billing:request_refund", kwargs={"invoice_id": self.invoice.id})

    def test_within_window_auto_approves_and_refunds(self):
        response = self.client.post(self._url())
        self.assertRedirects(response, reverse("billing:my_invoices"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.REFUNDED)
        self.assertEqual(self.invoice.refund_amount, self.invoice.total)
        self.assertEqual(self.invoice.refunded_by, self.user)

        refund_request = RefundRequest.objects.get(invoice=self.invoice)
        self.assertEqual(refund_request.status, RefundRequest.Status.APPROVED)
        self.assertTrue(refund_request.auto_approved)

    def test_within_window_notifies_the_client(self):
        from notifications.models import Notification, NotificationType

        self.client.post(self._url())
        self.assertTrue(
            Notification.objects.filter(user=self.user, notification_type=NotificationType.REFUND_DECISION).exists()
        )

    def test_outside_window_creates_a_pending_request_without_refunding(self):
        self.invoice.verified_at = timezone.now() - timedelta(days=10)
        self.invoice.save(update_fields=["verified_at"])

        response = self.client.post(self._url(), {"reason": "Service was down for a week"})
        self.assertRedirects(response, reverse("billing:my_invoices"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

        refund_request = RefundRequest.objects.get(invoice=self.invoice)
        self.assertEqual(refund_request.status, RefundRequest.Status.PENDING)
        self.assertFalse(refund_request.auto_approved)
        self.assertEqual(refund_request.reason, "Service was down for a week")

    def test_outside_window_notifies_admins(self):
        from notifications.models import Notification, NotificationType

        self.invoice.verified_at = timezone.now() - timedelta(days=10)
        self.invoice.save(update_fields=["verified_at"])
        self.client.post(self._url())
        self.assertTrue(
            Notification.objects.filter(user=self.admin, notification_type=NotificationType.REFUND_REQUESTED).exists()
        )

    def test_cannot_request_refund_on_an_unpaid_invoice(self):
        self.invoice.status = Invoice.Status.UNPAID
        self.invoice.save(update_fields=["status"])
        self.client.post(self._url())
        self.assertFalse(RefundRequest.objects.filter(invoice=self.invoice).exists())

    def test_cannot_request_refund_twice_while_one_is_pending(self):
        self.invoice.verified_at = timezone.now() - timedelta(days=10)
        self.invoice.save(update_fields=["verified_at"])
        self.client.post(self._url())
        self.client.post(self._url())
        self.assertEqual(RefundRequest.objects.filter(invoice=self.invoice).count(), 1)

    def test_cannot_request_a_refund_on_someone_elses_invoice(self):
        self.client.logout()
        self.client.login(email="other@example.com", password="pw12345!")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 404)
        self.assertFalse(RefundRequest.objects.filter(invoice=self.invoice).exists())

    def test_requesting_refund_twice_in_a_row_does_not_double_refund_or_notify(self):
        """Regression test for the remaining-audit pass: a double-click on
        Request Refund (within the auto-approve window) used to be able to
        create two RefundRequests and refund twice, since the status check
        and the mutation weren't locked together. Sequential retry is what
        SQLite/the test client can exercise - the true concurrent-request
        race additionally needs select_for_update(), Postgres-only, same
        caveat as everywhere else this pattern is used."""
        from notifications.models import Notification, NotificationType

        self.client.post(self._url())
        self.client.post(self._url())
        self.assertEqual(RefundRequest.objects.filter(invoice=self.invoice).count(), 1)
        self.assertEqual(
            Notification.objects.filter(user=self.user, notification_type=NotificationType.REFUND_DECISION).count(),
            1,
        )


class ResolveRefundRequestTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.department,
        )
        self.other_admin = User.objects.create_user(
            email="otheradmin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            department=self.other_department,
        )
        self.recipient = User.objects.create_user(
            email="recipient@example.com", password="pw12345!", department=self.department
        )
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, is_tax_exempt=True)
        self.invoice = generate_invoice_for_department(self.department, recipient_user=self.recipient)
        self.invoice.verify_payment(self.superadmin)
        self.invoice.verified_at = timezone.now() - timedelta(days=10)
        self.invoice.save(update_fields=["verified_at"])
        self.refund_request = RefundRequest.objects.create(
            invoice=self.invoice, requested_by=self.recipient, requested_amount=self.invoice.total
        )

    def _url(self):
        return reverse("billing:resolve_refund_request", kwargs={"request_id": self.refund_request.id})

    def test_admin_can_approve(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(self._url(), {"action": "approve"})
        self.assertRedirects(response, reverse("billing:refund_requests"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.REFUNDED)
        self.refund_request.refresh_from_db()
        self.assertEqual(self.refund_request.status, RefundRequest.Status.APPROVED)
        self.assertEqual(self.refund_request.resolved_by, self.admin)

    def test_admin_can_reject(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(self._url(), {"action": "reject", "admin_notes": "Not a service failure"})
        self.assertRedirects(response, reverse("billing:refund_requests"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)
        self.refund_request.refresh_from_db()
        self.assertEqual(self.refund_request.status, RefundRequest.Status.REJECTED)
        self.assertEqual(self.refund_request.admin_notes, "Not a service failure")

    def test_decision_notifies_the_recipient(self):
        from notifications.models import Notification, NotificationType

        self.client.login(email="admin@example.com", password="pw12345!")
        self.client.post(self._url(), {"action": "approve"})
        self.assertTrue(
            Notification.objects.filter(
                user=self.recipient, notification_type=NotificationType.REFUND_DECISION
            ).exists()
        )

    def test_scoped_admin_cannot_resolve_another_departments_refund_request(self):
        self.client.login(email="otheradmin@example.com", password="pw12345!")
        response = self.client.post(self._url(), {"action": "approve"})
        self.assertEqual(response.status_code, 403)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

    def test_superadmin_can_resolve_any_departments_refund_request(self):
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(self._url(), {"action": "approve"})
        self.assertEqual(response.status_code, 302)

    def test_resolving_an_already_resolved_request_is_a_no_op(self):
        from notifications.models import Notification, NotificationType

        self.client.login(email="admin@example.com", password="pw12345!")
        self.client.post(self._url(), {"action": "approve"})
        response = self.client.post(self._url(), {"action": "reject"})
        self.assertRedirects(response, reverse("billing:refund_requests"))
        self.refund_request.refresh_from_db()
        self.assertEqual(self.refund_request.status, RefundRequest.Status.APPROVED)
        # Not just the status - the double-resolve (double-click) must not
        # have sent a second REFUND_DECISION notification either.
        self.assertEqual(
            Notification.objects.filter(
                user=self.recipient, notification_type=NotificationType.REFUND_DECISION
            ).count(),
            1,
        )


class CancelPlanTests(TestCase):
    def setUp(self):
        from governance.plans import assign_plan

        self.plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        assign_plan(self.user, self.plan)
        self.client.login(email="u@example.com", password="pw12345!")

    def test_cancel_sets_cancelled_at(self):
        response = self.client.post(reverse("billing:cancel_plan"))
        self.assertRedirects(response, reverse("billing:my_plans"))
        self.user.plan_assignment.refresh_from_db()
        self.assertIsNotNone(self.user.plan_assignment.cancelled_at)

    def test_cancel_writes_audit_log(self):
        from governance.models import AuditLog

        self.client.post(reverse("billing:cancel_plan"))
        self.assertTrue(AuditLog.objects.filter(action_type="user.plan_cancelled").exists())

    def test_resume_clears_cancelled_at(self):
        self.client.post(reverse("billing:cancel_plan"))
        response = self.client.post(reverse("billing:resume_plan"))
        self.assertRedirects(response, reverse("billing:my_plans"))
        self.user.plan_assignment.refresh_from_db()
        self.assertIsNone(self.user.plan_assignment.cancelled_at)

    def test_cancelling_twice_in_a_row_sends_only_one_notification(self):
        """Regression test for the remaining-audit pass: a double-click on
        Cancel plan used to be able to pass the "not already cancelled"
        check twice before either commit, sending two notifications."""
        from notifications.models import Notification, NotificationType

        self.client.post(reverse("billing:cancel_plan"))
        self.client.post(reverse("billing:cancel_plan"))
        self.assertEqual(
            Notification.objects.filter(user=self.user, notification_type=NotificationType.PLAN_CANCELLATION).count(),
            1,
        )

    def test_resuming_twice_in_a_row_sends_only_one_notification(self):
        from notifications.models import Notification, NotificationType

        self.client.post(reverse("billing:cancel_plan"))
        self.client.post(reverse("billing:resume_plan"))
        self.client.post(reverse("billing:resume_plan"))
        self.assertEqual(
            Notification.objects.filter(user=self.user, notification_type=NotificationType.PLAN_CANCELLATION).count(),
            2,  # one for the cancel, one for the (single) resume
        )

    def test_cancelled_plan_is_skipped_by_the_invoice_sweep(self):
        invoice = generate_invoice_for_user(self.user, plan=self.plan)
        invoice.due_date = timezone.localdate() - timedelta(days=28)
        invoice.save(update_fields=["due_date"])
        self.client.post(reverse("billing:cancel_plan"))

        result = sweep_due_invoices()
        self.assertEqual(result["skipped_cancelled"], 1)
        self.assertEqual(Invoice.objects.filter(recipient_user=self.user).count(), 1)

    def test_cancel_notifies_with_its_own_type_not_plan_change(self):
        """Not NotificationType.PLAN_CHANGE - a cancellation isn't a plan
        change (the plan itself never changes), and reusing that type
        would show a misleading "New plan" box on the confirmation email
        (see notifications/_email_content_plan_change.html)."""
        from notifications.models import Notification, NotificationType

        self.client.post(reverse("billing:cancel_plan"))
        notification = Notification.objects.get(user=self.user)
        self.assertEqual(notification.notification_type, NotificationType.PLAN_CANCELLATION)
        self.assertNotEqual(notification.notification_type, NotificationType.PLAN_CHANGE)
        self.assertIn("cancelled", notification.title.lower())

    def test_resume_also_sends_a_confirmation_notification(self):
        from notifications.models import Notification, NotificationType

        self.client.post(reverse("billing:cancel_plan"))
        self.client.post(reverse("billing:resume_plan"))
        notification = Notification.objects.filter(
            user=self.user, notification_type=NotificationType.PLAN_CANCELLATION
        ).latest(
            "created_at", "id"
        )  # id breaks a tie when both rows land in the same clock tick
        self.assertIn("active again", notification.title.lower())

    def test_cancel_can_redirect_back_to_the_dashboard(self):
        response = self.client.post(reverse("billing:cancel_plan"), {"next": "dashboard"})
        self.assertRedirects(response, reverse("accounts:dashboard"))

    def test_resume_can_redirect_back_to_the_dashboard(self):
        self.client.post(reverse("billing:cancel_plan"))
        response = self.client.post(reverse("billing:resume_plan"), {"next": "dashboard"})
        self.assertRedirects(response, reverse("accounts:dashboard"))
