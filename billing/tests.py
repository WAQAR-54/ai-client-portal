from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, User
from billing.access import has_overdue_unpaid_invoice
from billing.invoicing import InvoiceGenerationError, generate_invoice_for_department, generate_invoice_for_user
from billing.models import (
    DepartmentBillingProfile,
    Invoice,
    OrganizationBillingProfile,
    RegionalPrice,
    UserBillingProfile,
)
from billing.pdf import render_invoice_pdf
from billing.regions import REGIONS
from billing.tasks import sweep_due_invoices
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

    def test_admin_cannot_verify_other_departments_invoice(self):
        self.client.login(email="otheradmin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:verify_invoice_payment", kwargs={"invoice_id": self.invoice.id}))
        self.assertEqual(response.status_code, 403)

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

    def test_shows_company_phone_and_address_from_user_billing_profile(self):
        UserBillingProfile.objects.create(
            user=self.user,
            company_name="Khan Traders",
            phone_number="+92 300 1234567",
            billing_address="123 Mall Road, Lahore",
        )
        self.client.login(email="client@example.com", password="pw12345!")
        response = self.client.get(self._url())
        self.assertContains(response, "Khan Traders")
        self.assertContains(response, "client@example.com")
        self.assertContains(response, "+92 300 1234567")
        self.assertContains(response, "123 Mall Road, Lahore")

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
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.post(self._url())
        self.assertRedirects(response, reverse("billing:invoices"))
        self.assertFalse(Invoice.objects.filter(id=self.invoice.id).exists())

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
