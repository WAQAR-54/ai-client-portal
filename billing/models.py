from decimal import Decimal

from django.db import models
from django.utils import timezone

from accounts.models import Department
from billing.tax_rules import tax_rule_for_country
from governance.models import Plan


class RegionalPrice(models.Model):
    """What a client organization (a Department, see accounts.Department)
    actually pays for a Plan in one region - set exactly by a SuperAdmin,
    never auto-converted from another region's price. `region_code` is
    always one of billing.regions.ALL_REGIONS, enforced at the form layer
    (not a DB-level choices= constraint, so a region can be added to the
    registry without a migration).

    extra_team_price lives on this same row (not a separate per-team
    model) because it's denominated in the same region/currency as
    `price` - a department billed in PKR pays its extra-team fee in PKR
    too. Null means "no extra-team charge configured for this region" -
    an over-the-included-count department simply isn't charged for it
    yet, rather than the invoice sweep guessing a number.
    """

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="regional_prices")
    region_code = models.CharField(max_length=10)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    extra_team_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plan", "region_code"], name="unique_plan_region_price"),
        ]
        ordering = ["plan_id", "region_code"]

    def __str__(self):
        return f"{self.plan.name} / {self.region_code}: {self.price}"


class OrganizationBillingProfile(models.Model):
    """Singleton (always pk=1, via .load()) holding the OPERATOR's own
    payment/account details - shown on every invoice footer so a client
    knows where to send payment. Same singleton pattern as governance's
    SecuritySettings/ComplianceSettings/SiteBranding."""

    bank_name = models.CharField(max_length=200, blank=True)
    account_title = models.CharField(max_length=200, blank=True)
    account_number = models.CharField(max_length=100, blank=True)
    swift_code = models.CharField(max_length=50, blank=True)
    payment_note = models.TextField(blank=True)

    class Meta:
        verbose_name = "Organization billing profile"
        verbose_name_plural = "Organization billing profile"

    def __str__(self):
        return "Organization billing profile"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class DepartmentBillingProfile(models.Model):
    """The billed-client side of an invoice - one per Department (see
    accounts.Department), which is this product's billable-client unit
    (a Department's individual users keep their own governance.Plan for
    AI-usage limits; this is what the department as a whole pays for its
    subscription). Deliberately has no logo field: the mockup this was
    built from labels the logo "your logo" (the operator's), which already
    exists as governance.models.SiteBranding.logo/site_name - invoices
    reuse that directly instead of a duplicate per-department field."""

    department = models.OneToOneField(Department, on_delete=models.CASCADE, related_name="billing_profile")
    company_name = models.CharField(max_length=200, blank=True)
    country = models.CharField(max_length=10, blank=True)
    billing_address = models.TextField(blank=True)
    tax_id = models.CharField(max_length=100, blank=True)
    is_tax_exempt = models.BooleanField(default=False)
    custom_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Percent. Overrides the country's default tax rate when set.",
    )
    auto_generate_invoices = models.BooleanField(default=True)

    class ReminderSchedule(models.IntegerChoices):
        NONE = 0, "No reminder"
        DAYS_3 = 3, "3 days after due"
        DAYS_7 = 7, "7 days after due"

    reminder_days_after_due = models.PositiveIntegerField(
        choices=ReminderSchedule.choices, default=ReminderSchedule.NONE
    )

    def __str__(self):
        return f"{self.department.name} billing profile"

    def effective_tax_rate(self):
        """The tax percentage actually applied to this department's
        invoices - exempt beats a custom override, which beats the
        country default. Used at invoice-generation time (Milestone 4/5)
        and by the settings page's live preview."""
        if self.is_tax_exempt:
            return Decimal("0")
        if self.custom_tax_rate is not None:
            return self.custom_tax_rate
        return tax_rule_for_country(self.country)["tax_rate"]


class Invoice(models.Model):
    """One billing-period invoice for a Department (the billable-client
    unit throughout this feature). plan/currency/amounts are snapshotted
    at creation time, not live pointers - a department's later plan change
    or a SuperAdmin editing RegionalPrice afterward must never rewrite the
    numbers on a past invoice.

    Created either by hand (billing.views.generate_invoice, this
    milestone) or automatically (billing.tasks.sweep_due_invoices,
    Milestone 5) - both go through billing.invoicing.generate_invoice_for_
    department so the money math has exactly one implementation."""

    class Status(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PAID = "paid", "Paid"

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="invoices")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="invoices")
    invoice_number = models.CharField(max_length=30, unique=True, editable=False)
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    currency = models.CharField(max_length=10)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNPAID)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issue_date", "-id"]

    def __str__(self):
        return self.invoice_number

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            self.invoice_number = self._next_invoice_number()
        super().save(*args, **kwargs)

    @classmethod
    def _next_invoice_number(cls):
        # Zero-padded 4-digit sequence per calendar year, e.g. INV-2026-0001.
        # Looked up by PK order (not a string sort on invoice_number, which
        # would only coincidentally match numeric order) so a deleted
        # invoice never causes a collision with the next one created.
        prefix = f"INV-{timezone.localdate().year}-"
        last = cls.objects.filter(invoice_number__startswith=prefix).order_by("-id").first()
        next_seq = int(last.invoice_number.rsplit("-", 1)[-1]) + 1 if last else 1
        return f"{prefix}{next_seq:04d}"
