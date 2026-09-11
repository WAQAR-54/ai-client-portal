from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from billing.models import RegionalPrice
from billing.regions import REGIONS
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
