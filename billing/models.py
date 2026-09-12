import secrets
from decimal import Decimal

from django.conf import settings
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

    extra_seat_price lives on this same row (not a separate per-seat
    model) because it's denominated in the same region/currency as
    `price` - a department billed in PKR pays its extra-seat fee in PKR
    too. Null means "no extra-seat charge configured for this region" -
    an over-the-included-count department simply isn't charged for it
    yet, rather than the invoice sweep guessing a number.
    """

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="regional_prices")
    region_code = models.CharField(max_length=10)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    extra_seat_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

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


class UserBillingProfile(models.Model):
    """The billed-client side of a department-less invoice (see
    billing.invoicing.generate_invoice_for_user) - the individual-user
    counterpart to DepartmentBillingProfile above. A user fills this in
    themselves from My Invoices; every field is optional since the Bill To
    block already has their name/email from the User record regardless of
    whether this profile exists."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="billing_profile")
    company_name = models.CharField(max_length=200, blank=True)
    phone_number = models.CharField(max_length=32, blank=True)
    billing_address = models.TextField(blank=True)

    def __str__(self):
        return f"Billing profile for {self.user}"


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
        # A user submitted a transaction ID and/or a screenshot (see
        # submit_payment_proof below) and it's waiting on an Admin/
        # SuperAdmin to check it - distinct from PAID so "the money's in"
        # is always a human decision, never the user's own claim.
        PENDING_VERIFICATION = "pending_verification", "Pending verification"
        PAID = "paid", "Paid"

    # Nullable: a department-less user (see billing.invoicing.
    # generate_invoice_for_user) is still billable directly - invoicing
    # doesn't require a Department, only a recipient_user. SET_NULL (not
    # CASCADE) for the same reason as recipient_user/verified_by below -
    # billing history must outlive a deleted Department.
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )
    # Who this invoice is actually billed to and who sees it under "My
    # Invoices" - SET_NULL (not CASCADE) so deleting a user account never
    # deletes billing history; a null recipient just means the invoice was
    # created before this field existed, or the recipient was since removed.
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="invoices")
    invoice_number = models.CharField(max_length=30, unique=True, editable=False)
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    currency = models.CharField(max_length=10)
    # [{"description": str, "amount": str}, ...] - e.g. the base plan
    # charge plus, when relevant, a separate "Extra members - N x price
    # (X total, Y included)" line - built once at generation time in
    # billing.invoicing so the itemized breakdown on the invoice detail
    # page always matches exactly what subtotal was computed from,
    # regardless of any later RegionalPrice/Plan change.
    line_items = models.JSONField(default=list, blank=True)
    # The headcount this invoice was actually billed against (department.
    # users.count() at generation time, or an explicit override - see
    # billing.invoicing.generate_invoice_for_department) - snapshotted
    # like everything else here so it always matches what line_items says,
    # regardless of the department's headcount changing later. Null only
    # for a plan with unlimited seats (seats_included=None), where no
    # headcount was ever consulted.
    seats_billed = models.PositiveIntegerField(null=True, blank=True)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.UNPAID)
    created_at = models.DateTimeField(auto_now_add=True)

    # Proof of payment, submitted by recipient_user against their own
    # unpaid invoice (billing.views.submit_payment_proof) - both optional
    # individually, but the view requires at least one of the two.
    submitted_transaction_id = models.CharField(max_length=200, blank=True)
    submitted_proof_image = models.ImageField(upload_to="invoice_proofs/", null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    # Who verified the submission (Approve -> PAID, or Reject -> UNPAID so
    # the recipient can resubmit) and when - billing.views.verify_invoice_
    # payment/reject_invoice_payment.
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    # An unguessable token for the public, no-login invoice view (see
    # billing.views.public_invoice_view) - so a client can be emailed or
    # sent a direct link without needing a portal account. Deliberately
    # not the primary key: invoice_number stays the meaningful sequential
    # identifier, this is purely an opaque sharing credential. Nullable at
    # the DB level only so existing rows can be backfilled by migration;
    # save() below guarantees every invoice has one from here on.
    share_token = models.CharField(max_length=48, unique=True, null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-issue_date", "-id"]

    def __str__(self):
        return self.invoice_number

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            self.invoice_number = self._next_invoice_number()
        if not self.share_token:
            self.share_token = secrets.token_urlsafe(24)
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

    def submit_payment_proof(self, *, transaction_id="", proof_image=None):
        """Recipient claims they've paid - moves to PENDING_VERIFICATION,
        never straight to PAID (billing.views.submit_payment_proof already
        validates at least one of transaction_id/proof_image is given and
        that this invoice is actually UNPAID before calling this)."""
        self.submitted_transaction_id = transaction_id
        if proof_image is not None:
            self.submitted_proof_image = proof_image
        self.submitted_at = timezone.now()
        self.status = self.Status.PENDING_VERIFICATION
        self.save(update_fields=["submitted_transaction_id", "submitted_proof_image", "submitted_at", "status"])

    def verify_payment(self, verifier):
        self.status = self.Status.PAID
        self.verified_by = verifier
        self.verified_at = timezone.now()
        self.save(update_fields=["status", "verified_by", "verified_at"])

    def reject_payment(self, verifier):
        # Back to UNPAID (not left at PENDING_VERIFICATION) so the
        # recipient can see the rejection and resubmit - verified_by/
        # verified_at still record who reviewed it and when.
        self.status = self.Status.UNPAID
        self.verified_by = verifier
        self.verified_at = timezone.now()
        self.save(update_fields=["status", "verified_by", "verified_at"])


def billing_profile_for_invoice(invoice):
    """The billed-client side of the Bill To block - a DepartmentBillingProfile
    for a departmental invoice, or the individual recipient's own
    UserBillingProfile for a department-less one (see billing.invoicing.
    generate_invoice_for_user). Shared by billing/views.py and billing/pdf.py
    so the on-screen page, the PDF, and the public share link never
    disagree about which profile an invoice's Bill To/Payment Details come
    from. get_or_create rather than a plain fetch so a user/department
    filling this in for the first time never 404s; None only for the edge
    case of an invoice with neither (predates recipient_user existing at
    all)."""
    if invoice.department_id is not None:
        profile, _created = DepartmentBillingProfile.objects.get_or_create(department=invoice.department)
        return profile
    if invoice.recipient_user_id is not None:
        profile, _created = UserBillingProfile.objects.get_or_create(user=invoice.recipient_user)
        return profile
    return None
