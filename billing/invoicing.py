"""Money math for turning a Department's subscription into one Invoice -
the single implementation shared by the manual "Generate invoice" action
(billing.views.generate_invoice, Milestone 4) and the scheduled sweep
(billing.tasks.sweep_due_invoices, Milestone 5), so the two can never
compute a total differently.
"""

import calendar
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from billing.models import DepartmentBillingProfile, Invoice, RegionalPrice
from billing.regions import currency_for_region
from billing.tax_rules import tax_rule_for_country

DEFAULT_DUE_IN_DAYS = 14

# The self-checkout "Duration" picker's Custom option (billing.views.checkout_plan) - a sensible upper
# bound on a one-time custom month count, so a customer (or a scripted abuse attempt) can't submit an
# absurd value like 999999 and get an invoice for it. Named/documented here rather than an inline
# magic number, same reasoning as DEFAULT_DUE_IN_DAYS above; there is no existing Plan/billing setting
# for this in the project already (checked governance.models.Plan and every billing profile model), so
# this is the one place it's defined - change it here, not at either call site below.
MAX_CUSTOM_DURATION_MONTHS = 36


class InvoiceGenerationError(ValueError):
    """Raised when a department can't be invoiced yet - a clear, specific
    reason a view can show the SuperAdmin, not a generic failure."""


def _quantize(amount):
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def add_calendar_months(start_date, months):
    """start_date + `months` calendar months, e.g. Jan 15 + 8 -> Sep 15. Clamps the day to the target
    month's real length instead of raising (Jan 31 + 1 month -> Feb 28, or 29 in a leap year) - the
    standard calendar-month-arithmetic pattern, no extra dependency needed for it. Works on both
    date and datetime (whichever `start_date` is - .replace() preserves the type and, for a datetime,
    the time-of-day)."""
    month_index = start_date.month - 1 + months
    year = start_date.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start_date.day, calendar.monthrange(year, month)[1])
    return start_date.replace(year=year, month=month, day=day)


def validate_duration_months(months):
    """The server's own independent check, same validation shape whether the caller is the
    self-checkout view (billing.views.checkout_plan, which already validated the raw POST value
    before ever calling in here) or any other future caller - this function never trusts that a
    positive-int-in-range value actually reached it, per the spec's own "never trust the frontend's
    duration" instruction extended to defense in depth at this layer too."""
    if not isinstance(months, int) or isinstance(months, bool) or months < 1:
        raise InvoiceGenerationError("Duration must be a positive whole number of months.")
    if months > MAX_CUSTOM_DURATION_MONTHS:
        raise InvoiceGenerationError(f"Duration can't be more than {MAX_CUSTOM_DURATION_MONTHS} months.")


def _apply_duration_months(subtotal, line_items, months):
    """Scales a subtotal/line_items pair (still just "one billing period's worth", computed identically
    to how this file already priced a plan before duration options existed) up to the actual number of
    months being billed for - one multiplication point shared by both generator functions below, so
    "the total is the per-month price times the number of months" can never be computed two different
    ways. A no-op for months=1 (every pre-existing caller), by construction: multiplying by 1 changes
    nothing, so this never has to special-case "was a duration actually chosen or not"."""
    if months == 1:
        return subtotal, line_items
    scaled_items = [
        {"description": f"{item['description']} × {months} months", "amount": str(Decimal(item["amount"]) * months)}
        for item in line_items
    ]
    return subtotal * months, scaled_items


# Self-checkout upgrade/change credit (billing.views.checkout_plan): the standard "daily rate x days
# remaining" proration convention (a fixed 30-day month for the rate itself - the same simplification
# most subscription billers use so the credit is predictable and doesn't shrink/grow depending on
# which calendar month it happens to fall in; days_remaining itself still comes from real calendar
# dates, only the per-day RATE uses this fixed denominator). No proration method already existed in
# this project (checked governance/billing models and every legal/docs policy page) - this is the one
# place it's defined.
UPGRADE_CREDIT_DAYS_PER_MONTH = 30


def _compute_upgrade_credit(user, new_plan):
    """The unused-value credit for switching a self-checkout user from their current plan to
    `new_plan`, per the spec's own worked example (current plan price/month x days remaining / 30).
    Returns (credit: Decimal, previous_plan: Plan|None, days_remaining: int) - previous_plan is None
    (credit always 0) for every case that isn't a genuine upgrade/change:
    - no assignment yet, or the assignment has no real fixed-term expiry (never bought via self-
      checkout, or on the org's default/free plan) - nothing to credit.
    - the SAME plan is being picked again - see checkout_plan's own docstring: that is a renewal
      (stacks days onto the existing expiry instead), not an upgrade, and must never ALSO get a
      price credit for time it already keeps via that stacking (the abuse the spec explicitly warns
      about: "do not allow users to ... manipulate the credit" - crediting the same unused time twice,
      once as extra days and once as a price discount, would be exactly that).
    - the current plan has already expired - there is no unused value left to credit.
    - no real PAID invoice for the current plan can be found - the credit is always capped at what
      the user actually paid (never more, however the daily-rate math comes out), so with nothing on
      file there is nothing to credit."""
    from governance.plans import get_assignment

    assignment = get_assignment(user)
    if assignment is None or assignment.plan_id == new_plan.id or assignment.expires_at is None:
        return Decimal("0"), None, 0

    now = timezone.now()
    if assignment.expires_at <= now:
        return Decimal("0"), None, 0
    days_remaining = (assignment.expires_at - now).days
    if days_remaining <= 0:
        return Decimal("0"), None, 0

    source_invoice = (
        Invoice.objects.filter(recipient_user=user, plan=assignment.plan, status=Invoice.Status.PAID)
        .order_by("-issue_date", "-id")
        .first()
    )
    if source_invoice is None:
        return Decimal("0"), None, 0

    daily_rate = source_invoice.monthly_price() / UPGRADE_CREDIT_DAYS_PER_MONTH
    credit = _quantize(daily_rate * days_remaining)
    # Explicit abuse-prevention cap, on top of the formula's own natural bound: never credit more
    # than the user actually paid for that plan, however the day-rate math comes out.
    credit = min(credit, source_invoice.total)
    return credit, assignment.plan, days_remaining


def _plan_line_item_description(plan):
    """ "Advanced Plan — Research, Document generation, ..." - reported
    directly: a department-less invoice's one line item used to read
    "Advanced — someone@example.com", the recipient's own email
    standing in as if it were the item description (confusing, and
    redundant with the Bill To block, which already has that address).
    Reuses governance.plans.plan_capability_summary - the exact same
    capability list already shown on the Plans pages - so this always
    reflects what the plan actually includes, rather than only working
    when a SuperAdmin has separately filled in Plan.description (most
    plans never have). Trims each label's parenthetical detail (e.g.
    "Document generation (Word/Excel/PowerPoint/PDF)" -> "Document
    generation") - a plain feature name reads better in a one-line
    invoice item than the fuller Plans-page wording."""
    from governance.plans import plan_capability_summary

    feature_names = [row["label"].split(" (")[0] for row in plan_capability_summary(plan) if row["included"]]
    if feature_names:
        return f"{plan.name} Plan — {', '.join(feature_names)}"
    return f"{plan.name} Plan"


def generate_invoice_for_department(
    department,
    recipient_user=None,
    *,
    plan=None,
    seat_count=None,
    region_code=None,
    due_in_days=DEFAULT_DUE_IN_DAYS,
    months=1,
    is_fixed_term=False,
):
    """Build and save one Invoice, billed to `recipient_user` (the person
    who sees it under "My Invoices" and submits payment proof - see
    billing.views.generate_invoice).

    `months`: how many calendar months this invoice actually covers - 1 for every existing caller
    (unchanged behavior), or a validated positive int up to MAX_CUSTOM_DURATION_MONTHS when a customer
    picked a duration on the self-checkout page (billing.views.checkout_plan, which validates the raw
    value before it ever reaches here - validate_duration_months below is the independent server-side
    check this function makes regardless of what already ran in the view, per the spec's own "the
    server MUST independently validate" instruction). Multiplies the whole subtotal (base charge, and
    any extra-seat charge) by this - see _apply_duration_months. Stored on the created Invoice as
    duration_months, which is also what Invoice.subscription_expiry_date() reads.

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
    given nor on the department), that plan has no price set for the
    department's billing region, or months fails validation."""
    validate_duration_months(months)
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

    subtotal, line_items = _apply_duration_months(subtotal, line_items, months)

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
        duration_months=months,
        is_fixed_term=is_fixed_term,
        currency=currency_for_region(region_code),
        line_items=line_items,
        seats_billed=actual_seats,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
        status=Invoice.Status.UNPAID,
    )


def generate_invoice_for_team(team, *, plan=None, region_code=None, due_in_days=None):
    """One invoice for a specific Team, billed to that team's own Manager
    (accounts.Team.manager) - not the department-wide "pick any user"
    flow generate_invoice_for_department's caller normally drives.
    Reported directly: there was no way to bill a team on its own at
    all, and the department-wide seat_count (department.users.count())
    doesn't mean anything for "this one team's headcount."

    seat_count is ALWAYS team.members.count() - computed fresh from the
    real accounts.Team roster every time this runs, never a stored or
    manually-typed number, so it can never drift from reality (the exact
    complaint: "increasing team size doesn't get calculated in"). Delegates
    the actual money math (plan resolution, regional price, tax, the
    existing extra-seat-over-plan.seats_included line item) straight to
    generate_invoice_for_department - reused as-is, not reimplemented,
    since a team is still billed against its department's regional price/
    tax profile.

    Raises InvoiceGenerationError if the team has no manager assigned yet
    (there'd be no one to send it to) - same clear-reason-not-generic-
    failure convention as every other case that function already raises."""
    if team.manager_id is None:
        raise InvoiceGenerationError(f'"{team.name}" has no manager assigned yet - assign one before invoicing it.')

    seat_count = team.members.count()
    return generate_invoice_for_department(
        team.department,
        recipient_user=team.manager,
        plan=plan,
        seat_count=seat_count,
        region_code=region_code,
        due_in_days=due_in_days if due_in_days is not None else DEFAULT_DUE_IN_DAYS,
    )


def _effective_due_in_days(plan, due_in_days):
    if due_in_days is not None:
        return due_in_days
    if plan.is_demo and plan.demo_duration_days:
        return plan.demo_duration_days
    return DEFAULT_DUE_IN_DAYS


def generate_invoice_for_user(
    user, *, plan=None, seat_count=None, due_in_days=None, region_code=None, months=1, is_fixed_term=False
):
    """Build and save one Invoice billed directly to `user` - the
    department-optional counterpart to generate_invoice_for_department.
    Department assignment is a separate, optional, admin-driven action in
    this app (see accounts.views.signup_view/accounts.signals) - invoicing
    must never require one, so this is the single implementation behind
    both the automatic "welcome invoice" on account creation (accounts.
    signals.generate_welcome_invoice_on_creation) and the recurring
    monthly sweep (billing.tasks.sweep_due_invoices).

    `months`: see generate_invoice_for_department's own docstring - identical meaning, passed straight
    through when delegating there. This is the parameter billing.views.checkout_plan actually calls
    with the customer's chosen (and independently, server-side re-validated) duration.

    Plan resolution: `plan` argument > `user.department.plan` (if the user
    has a department and it has a plan) > the user's own individual
    governance.UserPlanAssignment.plan (assigned to every user on creation
    - see governance.plans.assign_default_plan_if_missing, always the
    seeded "Demo" plan by default). Raises InvoiceGenerationError if none
    of those resolve to a plan.

    `seat_count` is passed straight through to generate_invoice_for_department
    when delegating (see below) - it's a no-op otherwise, since a
    department-less individual always bills exactly one seat regardless.

    `is_fixed_term`: True only for a genuine self-checkout purchase (billing.views.checkout_plan) -
    see Invoice.is_fixed_term's own field comment for exactly what this changes once the invoice is
    paid. Upgrade/change credit (_compute_upgrade_credit) is only ever computed on THIS
    department-less path, not when delegating to generate_invoice_for_department below - self-
    checkout is a personal purchase against the user's own governance.UserPlanAssignment, a
    different concept from a department's own subscription, which has no "switch and get credited"
    flow in this pass.

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

    validate_duration_months(months)
    resolved_due_in_days = _effective_due_in_days(resolved_plan, due_in_days)

    if department_has_plan:
        return generate_invoice_for_department(
            user.department,
            recipient_user=user,
            plan=resolved_plan,
            seat_count=seat_count,
            region_code=region_code,
            due_in_days=resolved_due_in_days,
            months=months,
            is_fixed_term=is_fixed_term,
        )

    resolved_region_code = region_code or "ROW"
    regional_price = RegionalPrice.objects.filter(plan=resolved_plan, region_code=resolved_region_code).first()
    if regional_price is None or regional_price.price is None:
        raise InvoiceGenerationError(f"{resolved_plan.name} has no price set for {resolved_region_code} yet.")

    subtotal = regional_price.price
    line_items = [{"description": _plan_line_item_description(resolved_plan), "amount": str(regional_price.price)}]
    # A department-less invoice always bills exactly one person - unlike
    # generate_invoice_for_department's actual_seats, there's no team to
    # count here, so this is never conditional on plan.seats_included.
    seats_billed = 1

    subtotal, line_items = _apply_duration_months(subtotal, line_items, months)

    # Upgrade/change credit - only ever non-zero for a fixed-term (self-checkout) purchase of a
    # DIFFERENT plan than the one currently assigned; see _compute_upgrade_credit's own docstring for
    # every case that stays at zero (same plan, no expiry yet, already expired, nothing paid on
    # file). Applied to the TAXABLE amount, not just subtracted from the final total - the credit is
    # a real discount on what's being charged, not a separate payment applied after tax.
    credit_applied = Decimal("0")
    previous_plan = None
    if is_fixed_term:
        credit_applied, previous_plan, days_remaining = _compute_upgrade_credit(user, resolved_plan)
        if credit_applied > 0:
            line_items.append(
                {
                    "description": f"Credit — {days_remaining} unused day(s) on {previous_plan.name}",
                    "amount": str(-credit_applied),
                }
            )
    taxable_amount = max(Decimal("0"), subtotal - credit_applied)

    tax_rate = tax_rule_for_country(resolved_region_code)["tax_rate"]
    tax_amount = _quantize(taxable_amount * tax_rate / Decimal("100"))
    total = taxable_amount + tax_amount

    issue_date = timezone.localdate()
    return Invoice.objects.create(
        department=None,
        recipient_user=user,
        plan=resolved_plan,
        previous_plan=previous_plan,
        credit_applied=credit_applied,
        issue_date=issue_date,
        due_date=issue_date + timedelta(days=resolved_due_in_days),
        duration_months=months,
        is_fixed_term=is_fixed_term,
        currency=currency_for_region(resolved_region_code),
        line_items=line_items,
        seats_billed=seats_billed,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
        status=Invoice.Status.UNPAID,
    )
