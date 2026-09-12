from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import Department, Team, User
from billing.invoicing import InvoiceGenerationError, generate_invoice_for_department
from billing.models import DepartmentBillingProfile, Invoice, OrganizationBillingProfile, RegionalPrice
from billing.regions import REGIONS
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
        self.plan = Plan.objects.create(name="Public Plan", teams_included=3)
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

    def test_saves_price_with_comma_stripped_and_teams_included(self):
        response = self.client.post(
            reverse("billing:update_plan_regional_pricing", kwargs={"plan_id": self.plan.id}),
            {"teams_included": "3", "price_PK": "8,900", "price_SA": "299", "price_AE": "299", "price_ROW": "32"},
        )
        self.assertRedirects(response, reverse("billing:regional_pricing"))

        self.plan.refresh_from_db()
        self.assertEqual(self.plan.teams_included, 3)
        pk_price = RegionalPrice.objects.get(plan=self.plan, region_code="PK")
        self.assertEqual(pk_price.price, Decimal("8900"))

    def test_blank_price_saves_as_none_not_zero(self):
        RegionalPrice.objects.create(plan=self.plan, region_code="PK", price=Decimal("100"))
        self.client.post(
            reverse("billing:update_plan_regional_pricing", kwargs={"plan_id": self.plan.id}),
            {"price_PK": "", "teams_included": ""},
        )
        pk_price = RegionalPrice.objects.get(plan=self.plan, region_code="PK")
        self.assertIsNone(pk_price.price)
        self.plan.refresh_from_db()
        self.assertIsNone(self.plan.teams_included)

    def test_saves_extra_team_price(self):
        self.client.post(
            reverse("billing:update_plan_regional_pricing", kwargs={"plan_id": self.plan.id}),
            {"teams_included": "3", "extra_team_price_PK": "2,500"},
        )
        pk_price = RegionalPrice.objects.get(plan=self.plan, region_code="PK")
        self.assertEqual(pk_price.extra_team_price, Decimal("2500"))

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
    plan pricing, tax, and the team-based-billing extra-teams line item."""

    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.plan = Plan.objects.create(name="Growth", teams_included=2)
        RegionalPrice.objects.create(
            plan=self.plan, region_code="AE", price=Decimal("100"), extra_team_price=Decimal("20")
        )
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, country="AE")

    def test_raises_when_no_plan_assigned(self):
        self.department.plan = None
        self.department.save(update_fields=["plan"])
        with self.assertRaises(InvoiceGenerationError):
            generate_invoice_for_department(self.department)

    def test_raises_when_plan_has_no_price_for_region(self):
        RegionalPrice.objects.filter(plan=self.plan, region_code="AE").update(price=None)
        with self.assertRaises(InvoiceGenerationError):
            generate_invoice_for_department(self.department)

    def test_basic_invoice_with_no_tax_no_extra_teams(self):
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=True)
        invoice = generate_invoice_for_department(self.department)
        self.assertEqual(invoice.subtotal, Decimal("100"))
        self.assertEqual(invoice.tax_amount, Decimal("0.00"))
        self.assertEqual(invoice.total, Decimal("100"))
        self.assertEqual(invoice.currency, "AED")
        self.assertEqual(invoice.status, Invoice.Status.UNPAID)
        self.assertTrue(invoice.invoice_number.startswith("INV-"))

    def test_applies_country_default_tax_rate(self):
        invoice = generate_invoice_for_department(self.department)
        # AE's default rate is 5% (billing/tax_rules.py) on a 100 subtotal.
        self.assertEqual(invoice.tax_rate, Decimal("5"))
        self.assertEqual(invoice.tax_amount, Decimal("5.00"))
        self.assertEqual(invoice.total, Decimal("105.00"))

    def test_extra_teams_beyond_included_count_are_billed(self):
        for i in range(4):
            Team.objects.create(name=f"Team {i}", department=self.department)
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=True)
        invoice = generate_invoice_for_department(self.department)
        # 4 teams, 2 included -> 2 extra x 20/team = 40 on top of the 100 base.
        self.assertEqual(invoice.subtotal, Decimal("140"))
        self.assertEqual(invoice.total, Decimal("140"))

    def test_extra_teams_not_billed_when_extra_team_price_unset(self):
        RegionalPrice.objects.filter(plan=self.plan, region_code="AE").update(extra_team_price=None)
        for i in range(4):
            Team.objects.create(name=f"Team {i}", department=self.department)
        DepartmentBillingProfile.objects.filter(department=self.department).update(is_tax_exempt=True)
        invoice = generate_invoice_for_department(self.department)
        self.assertEqual(invoice.subtotal, Decimal("100"))

    def test_unlimited_teams_included_never_bills_extra(self):
        self.plan.teams_included = None
        self.plan.save(update_fields=["teams_included"])
        for i in range(10):
            Team.objects.create(name=f"Team {i}", department=self.department)
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


class InvoiceListViewTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Sales")
        self.other_department = Department.objects.create(name="Support")
        self.plan = Plan.objects.create(name="Growth", teams_included=None)
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

        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.department, is_tax_exempt=True)
        self.invoice = generate_invoice_for_department(self.department)

        self.other_department.plan = self.plan
        self.other_department.save(update_fields=["plan"])
        DepartmentBillingProfile.objects.create(department=self.other_department, is_tax_exempt=True)
        self.other_invoice = generate_invoice_for_department(self.other_department)

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

    def test_plain_admin_cannot_toggle_status(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:toggle_invoice_status", kwargs={"invoice_id": self.invoice.id}))
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
        self.plan = Plan.objects.create(name="Growth")
        RegionalPrice.objects.create(plan=self.plan, region_code="ROW", price=Decimal("50"))
        self.department.plan = self.plan
        self.department.save(update_fields=["plan"])

        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True
        )
        self.client.login(email="super@example.com", password="pw12345!")

    def test_generates_invoice_for_department(self):
        response = self.client.post(reverse("billing:generate_invoice"), {"department_id": self.department.id})
        self.assertRedirects(response, reverse("billing:invoices"))
        self.assertTrue(Invoice.objects.filter(department=self.department).exists())

    def test_non_superadmin_cannot_generate(self):
        self.client.logout()
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.post(reverse("billing:generate_invoice"), {"department_id": self.department.id})
        self.assertEqual(response.status_code, 403)

    def test_missing_price_shows_error_message_and_creates_nothing(self):
        other_department = Department.objects.create(name="No Price Dept", plan=self.plan)
        RegionalPrice.objects.filter(plan=self.plan, region_code="ROW").update(price=None)
        response = self.client.post(
            reverse("billing:generate_invoice"), {"department_id": other_department.id}, follow=True
        )
        self.assertFalse(Invoice.objects.filter(department=other_department).exists())
        messages = list(response.context["messages"])
        self.assertTrue(any("no price" in str(m) for m in messages))
