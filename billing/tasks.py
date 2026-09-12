import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

BILLING_CYCLE_DAYS = 30
GENERATE_DAYS_BEFORE_DUE = 3


@shared_task
def sweep_due_invoices():
    """Daily beat task: generate each billable user's NEXT invoice a few
    days before it's due, on a rolling monthly cycle anchored to their own
    most recent invoice - not a separately-tracked "next billing date"
    field, so there's nothing that can desync from the invoices actually
    issued (mirrors governance.plans.get_plan_status's live-check
    philosophy - see billing.access.has_overdue_unpaid_invoice for the
    matching access-control side of this feature, which is likewise a
    live check with no dependency on this task having run).

    Users with zero invoices yet are skipped entirely - their first one
    is accounts.signals.generate_welcome_invoice_on_creation's job, not
    this sweep's."""
    from billing.invoicing import InvoiceGenerationError, generate_invoice_for_user
    from billing.models import DepartmentBillingProfile, Invoice

    today = timezone.localdate()
    generated = skipped_auto_generate_off = no_price = 0

    recipient_ids = (
        Invoice.objects.filter(recipient_user__isnull=False).values_list("recipient_user_id", flat=True).distinct()
    )

    for recipient_id in recipient_ids:
        latest = Invoice.objects.filter(recipient_user_id=recipient_id).order_by("-issue_date", "-id").first()
        if latest is None:
            continue

        next_due = latest.due_date + timedelta(days=BILLING_CYCLE_DAYS)
        if next_due > today + timedelta(days=GENERATE_DAYS_BEFORE_DUE):
            continue  # not yet in the generate-ahead window
        if Invoice.objects.filter(recipient_user_id=recipient_id, due_date=next_due).exists():
            continue  # already generated this cycle - avoids double-generation on a late/rerun sweep

        user = latest.recipient_user
        if user is None:
            continue

        if user.department_id:
            profile = DepartmentBillingProfile.objects.filter(department_id=user.department_id).first()
            if profile is not None and not profile.auto_generate_invoices:
                skipped_auto_generate_off += 1
                continue

        try:
            # Continues billing whatever plan the previous invoice was
            # for - a recurring cycle must never silently re-resolve to a
            # different plan (e.g. a department-less user's current
            # governance.UserPlanAssignment.plan, which is unrelated to
            # what they were actually being billed for last cycle).
            generate_invoice_for_user(user, plan=latest.plan, due_in_days=(next_due - today).days)
            generated += 1
        except InvoiceGenerationError:
            no_price += 1
            continue

    logger.info(
        "Invoice sweep: %d generated, %d skipped (auto-generate off), %d skipped (no price)",
        generated,
        skipped_auto_generate_off,
        no_price,
    )
    return {"generated": generated, "skipped_auto_generate_off": skipped_auto_generate_off, "no_price": no_price}
