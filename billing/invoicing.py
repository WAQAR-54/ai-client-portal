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
from billing.tax_rules import tax_rule_for_country

DEFAULT_DUE_IN_DAYS = 14


class InvoiceGenerationError(ValueError):
    """Raised when a department can't be invoiced yet - a clear, specific
    reason a view can show the SuperAdmin, not a generic failure."""


def _quantize(amount):
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def generate_invoice_for_department(
    department, recipient_user=None, *, plan=None, seat_count=None, region_code=None, due_in_days=DEFAULT_DUE_IN_DAYS
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
    `region_code` defaults to the department's own configured billing
    country but can likewise be overridden for a one-off invoice in a
    different region (e.g. billing a normally-Pakistan department in USD/
    ROW just this once) - this affects both the price/currency looked up
    AND the country-default tax tier (exempt/custom-rate still come from
    the department's own DepartmentBillingProfile regardless of region,
    same precedence as effective_tax_rate(), just with the override
    region standing in for "the department's country" in that one tier).

    Raises InvoiceGenerationError if there's no plan to bill (neither
    given nor on the department) or that plan has no price set for the
    department's billing region."""
    plan = plan or department.plan
    if plan is None:
        raise InvoiceGenerationError(f"{department.name} has no subscription plan assigned.")

    billing_profile, _created = DepartmentBillingProfile.objects.get_or_create(department=department)
    region_code = region_code or billing_profile.country or "ROW"

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
    actual_seats = None
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

    if billing_profile.is_tax_exempt:
        tax_rate = Decimal("0")
    elif billing_profile.custom_tax_rate is not None:
        tax_rate = billing_profile.custom_tax_rate
    else:
        tax_rate = tax_rule_for_country(region_code)["tax_rate"]
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
        seats_billed=actual_seats,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
        status=Invoice.Status.UNPAID,
    )


def _effective_due_in_days(plan, due_in_days):
    if due_in_days is not None:
        return due_in_days
    if plan.is_demo and plan.demo_duration_days:
        return plan.demo_duration_days
    return DEFAULT_DUE_IN_DAYS


def generate_invoice_for_user(user, *, plan=None, seat_count=None, due_in_days=None, region_code=None):
    """Build and save one Invoice billed directly to `user` - the
    department-optional counterpart to generate_invoice_for_department.
    Department assignment is a separate, optional, admin-driven action in
    this app (see accounts.views.signup_view/accounts.signals) - invoicing
    must never require one, so this is the single implementation behind
    both the automatic "welcome invoice" on account creation (accounts.
    signals.generate_welcome_invoice_on_creation) and the recurring
    monthly sweep (billing.tasks.sweep_due_invoices).

    Plan resolution: `plan` argument > `user.department.plan` (if the user
    has a department and it has a plan) > the user's own individual
    governance.UserPlanAssignment.plan (assigned to every user on creation
    - see governance.plans.assign_default_plan_if_missing, always the
    seeded "Demo" plan by default). Raises InvoiceGenerationError if none
    of those resolve to a plan.

    `seat_count` is passed straight through to generate_invoice_for_department
    when delegating (see below) - it's a no-op otherwise, since a
    department-less individual always bills exactly one seat regardless.

    When the user's department has its own configured plan, this simply
    delegates to generate_invoice_for_department (recipient_user=user) -
    reusing its region/tax/seat-billing logic entirely rather than
    duplicating it, so a departmental user's invoices are identical in
    shape whether triggered here or from the manual admin "Generate
    invoice" action. A user whose department has never been given a plan
    of its own is treated the same as a department-less user for billing
    purposes (see below) - deliberately NOT delegated to
    generate_invoice_for_department, which would otherwise silently
    create a blank DepartmentBillingProfile row for a department that was
    never actually set up for billing, just because one of its members
    happened to get a personal Demo invoice.

    With no department (or no departmental plan), there's no
    DepartmentBillingProfile to consult: region defaults to "ROW", tax to
    the plain country-default rate for that region (no exempt/custom-rate
    concept without a billing profile), exactly 1 seat is ever billed (a
    lone individual can't have "extra team members"), and the resulting
    Invoice.department is left null even if the user technically belongs
    to one, since this invoice isn't really about that department's
    subscription."""
    resolved_plan = plan
    department_has_plan = bool(user.department_id and user.department.plan_id)
    if resolved_plan is None and department_has_plan:
        resolved_plan = user.department.plan
    if resolved_plan is None:
        resolved_plan = getattr(getattr(user, "plan_assignment", None), "plan", None)
    if resolved_plan is None:
        raise InvoiceGenerationError(f"{user.email} has no plan assigned.")

    resolved_due_in_days = _effective_due_in_days(resolved_plan, due_in_days)

    if department_has_plan:
        return generate_invoice_for_department(
            user.department,
            recipient_user=user,
            plan=resolved_plan,
            seat_count=seat_count,
            region_code=region_code,
            due_in_days=resolved_due_in_days,
        )

    resolved_region_code = region_code or "ROW"
    regional_price = RegionalPrice.objects.filter(plan=resolved_plan, region_code=resolved_region_code).first()
    if regional_price is None or regional_price.price is None:
        raise InvoiceGenerationError(f"{resolved_plan.name} has no price set for {resolved_region_code} yet.")

    subtotal = regional_price.price
    line_items = [{"description": f"{resolved_plan.name} — {user.email}", "amount": str(regional_price.price)}]
    # A department-less invoice always bills exactly one person - unlike
    # generate_invoice_for_department's actual_seats, there's no team to
    # count here, so this is never conditional on plan.seats_included.
    seats_billed = 1

    tax_rate = tax_rule_for_country(resolved_region_code)["tax_rate"]
    tax_amount = _quantize(subtotal * tax_rate / Decimal("100"))
    total = subtotal + tax_amount

    issue_date = timezone.localdate()
    return Invoice.objects.create(
        department=None,
        recipient_user=user,
        plan=resolved_plan,
        issue_date=issue_date,
        due_date=issue_date + timedelta(days=resolved_due_in_days),
        currency=currency_for_region(resolved_region_code),
        line_items=line_items,
        seats_billed=seats_billed,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
        status=Invoice.Status.UNPAID,
    )
