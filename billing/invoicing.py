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


def generate_invoice_for_department(
    department, recipient_user=None, *, plan=None, seat_count=None, due_in_days=DEFAULT_DUE_IN_DAYS
):
    """Build and save one Invoice, billed to `recipient_user` (the person
    who sees it under "My Invoices" and submits payment proof - see
    billing.views.generate_invoice).

    `plan` defaults to `department.plan` but can be overridden per-invoice
    (a SuperAdmin/Admin explicitly billing this person for a different
    plan than the department's ongoing subscription - doesn't change
    department.plan itself). `seat_count` defaults to the department's
    actual current headcount (department.users.count()) but can likewise
    be overridden - e.g. invoicing ahead of new hires actually joining.

    Raises InvoiceGenerationError if there's no plan to bill (neither
    given nor on the department) or that plan has no price set for the
    department's billing region."""
    plan = plan or department.plan
    if plan is None:
        raise InvoiceGenerationError(f"{department.name} has no subscription plan assigned.")

    billing_profile, _created = DepartmentBillingProfile.objects.get_or_create(department=department)
    region_code = billing_profile.country or "ROW"

    regional_price = RegionalPrice.objects.filter(plan=plan, region_code=region_code).first()
    if regional_price is None or regional_price.price is None:
        raise InvoiceGenerationError(f"{plan.name} has no price set for {region_code} yet.")

    subtotal = regional_price.price
    line_items = [{"description": f"{plan.name} — {department.name}", "amount": str(regional_price.price)}]

    # Per-seat billing (see governance.models.Plan.seats_included /
    # RegionalPrice.extra_seat_price): a department with more people than
    # its plan includes is charged for each extra one, only if this
    # region has an extra-seat price configured. seat_count defaults to
    # actual accounts.User rows in the department (real seats/people, not
    # accounts.Team rows) but the caller may override it.
    if plan.seats_included is not None:
        actual_seats = department.users.count() if seat_count is None else seat_count
        extra_seats = max(0, actual_seats - plan.seats_included)
        if extra_seats > 0 and regional_price.extra_seat_price is not None:
            extra_amount = extra_seats * regional_price.extra_seat_price
            subtotal += extra_amount
            line_items.append(
                {
                    "description": (
                        f"Extra members — {extra_seats} × {regional_price.extra_seat_price} "
                        f"({actual_seats} total, {plan.seats_included} included)"
                    ),
                    "amount": str(extra_amount),
                }
            )

    tax_rate = billing_profile.effective_tax_rate()
    tax_amount = _quantize(subtotal * tax_rate / Decimal("100"))
    total = subtotal + tax_amount

    issue_date = timezone.localdate()
    return Invoice.objects.create(
        department=department,
        recipient_user=recipient_user,
        plan=plan,
        issue_date=issue_date,
        due_date=issue_date + timedelta(days=due_in_days),
        currency=currency_for_region(region_code),
        line_items=line_items,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
        status=Invoice.Status.UNPAID,
    )
