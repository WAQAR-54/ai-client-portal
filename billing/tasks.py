import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

BILLING_CYCLE_DAYS = 30
GENERATE_DAYS_BEFORE_DUE = 3
# Department-less invoices have no DepartmentBillingProfile to carry a
# reminder_days_after_due choice, so they get this fixed default -
# matches the "3 days" option already recommended elsewhere in this
# feature (see governance's own recurring-generation default).
DEFAULT_REMINDER_DAYS_AFTER_DUE = 3


def _reminder_days_for_invoice(invoice):
    """None means "no reminder for this invoice" - either a department
    explicitly configured ReminderSchedule.NONE (the field's own default,
    so most departments are opted out until an Admin turns it on), or a
    missing DepartmentBillingProfile row (same default). A department-
    less invoice always gets DEFAULT_REMINDER_DAYS_AFTER_DUE since there's
    no per-invoice setting to opt out with."""
    if invoice.department_id is None:
        return DEFAULT_REMINDER_DAYS_AFTER_DUE
    from billing.models import DepartmentBillingProfile

    profile = DepartmentBillingProfile.objects.filter(department_id=invoice.department_id).first()
    if profile is None or not profile.reminder_days_after_due:
        return None
    return profile.reminder_days_after_due


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


@shared_task
def send_overdue_reminders():
    """Daily beat task: emails each overdue-unpaid invoice's recipient a
    one-time dunning nudge once reminder_days_after_due days have passed
    since its due_date - a courtesy on top of, and entirely independent
    from, the actual access-control block (billing.access.
    has_overdue_unpaid_invoice fires the instant an invoice is overdue,
    with or without a reminder ever being configured or sent).

    Guarded by Invoice.reminder_sent_at (set the moment the email sends
    successfully), not a same-day date match - so a late or rerun sweep
    still catches every invoice that crossed its reminder threshold since
    the last run, exactly once each, rather than only ever firing on the
    one exact calendar day."""
    from billing.emails import send_overdue_reminder_email
    from billing.models import Invoice

    today = timezone.localdate()
    sent = skipped_no_reminder = skipped_no_recipient = failed = 0

    candidates = (
        Invoice.objects.filter(due_date__lt=today, reminder_sent_at__isnull=True)
        .exclude(status=Invoice.Status.PAID)
        .select_related("recipient_user", "department")
    )

    for invoice in candidates:
        reminder_days = _reminder_days_for_invoice(invoice)
        if not reminder_days:
            skipped_no_reminder += 1
            continue
        if today < invoice.due_date + timedelta(days=reminder_days):
            continue  # not yet at this invoice's own reminder threshold

        if invoice.recipient_user_id is None or not invoice.recipient_user.email:
            skipped_no_recipient += 1
            continue

        success, error = send_overdue_reminder_email(invoice)
        if not success:
            logger.warning("Overdue reminder failed for invoice %s: %s", invoice.invoice_number, error)
            failed += 1
            continue

        invoice.reminder_sent_at = timezone.now()
        invoice.save(update_fields=["reminder_sent_at"])
        sent += 1

    logger.info(
        "Overdue reminder sweep: %d sent, %d skipped (no reminder configured), %d skipped (no recipient email), "
        "%d failed",
        sent,
        skipped_no_reminder,
        skipped_no_recipient,
        failed,
    )
    return {
        "sent": sent,
        "skipped_no_reminder": skipped_no_reminder,
        "skipped_no_recipient": skipped_no_recipient,
        "failed": failed,
    }
