"""Money math for turning a Department's subscription into one Invoice -
the single implementation shared by the manual "Generate invoice" action
(billing.views.generate_invoice, Milestone 4) and the scheduled sweep
(billing.tasks.sweep_due_invoices, Milestone 5), so the two can never
compute a total differently.
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from billing.models import DepartmentBillingProfile, Invoice, RegionalPrice
from billing.regions import currency_for_region

DEFAULT_DUE_IN_DAYS = 14


class InvoiceGenerationError(ValueError):
    """Raised when a department can't be invoiced yet - a clear, specific
    reason a view can show the SuperAdmin, not a generic failure."""


def _quantize(amount):
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def generate_invoice_for_department(department, *, due_in_days=DEFAULT_DUE_IN_DAYS):
    """Build and save one Invoice for `department`'s current plan. Raises
    InvoiceGenerationError if the department has no plan assigned or that
    plan has no price set for the department's billing region."""
    plan = department.plan
    if plan is None:
        raise InvoiceGenerationError(f"{department.name} has no subscription plan assigned.")

    billing_profile, _created = DepartmentBillingProfile.objects.get_or_create(department=department)
    region_code = billing_profile.country or "ROW"

    regional_price = RegionalPrice.objects.filter(plan=plan, region_code=region_code).first()
    if regional_price is None or regional_price.price is None:
        raise InvoiceGenerationError(f"{plan.name} has no price set for {region_code} yet.")

    subtotal = regional_price.price

    # Team-based billing (see governance.models.Plan.teams_included /
    # RegionalPrice.extra_team_price): a department with more teams than
    # its plan includes is charged for each extra one, only if this
    # region has an extra-team price configured.
    if plan.teams_included is not None:
        extra_teams = max(0, department.teams.count() - plan.teams_included)
        if extra_teams > 0 and regional_price.extra_team_price is not None:
            subtotal += extra_teams * regional_price.extra_team_price

    tax_rate = billing_profile.effective_tax_rate()
    tax_amount = _quantize(subtotal * tax_rate / Decimal("100"))
    total = subtotal + tax_amount

    issue_date = timezone.localdate()
    return Invoice.objects.create(
        department=department,
        plan=plan,
        issue_date=issue_date,
        due_date=issue_date + timedelta(days=due_in_days),
        currency=currency_for_region(region_code),
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
        status=Invoice.Status.UNPAID,
    )
