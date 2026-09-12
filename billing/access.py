"""Billing-based access control - kept separate from governance/plans.py
(which owns Plan/UserPlanAssignment expiry state) since this queries
Invoice rows, a billing concept. A plain live read, same philosophy as
governance.plans.get_plan_status: no persisted "locked" flag, no
dependency on billing.tasks.sweep_due_invoices having run - the block
lifts the instant an Admin/SuperAdmin actually marks/approves the
invoice paid, checked fresh on every request.
"""

from django.utils import timezone

from billing.models import Invoice


def has_overdue_unpaid_invoice(user):
    """True when `user` has at least one Invoice billed to them whose
    due_date has passed and which isn't yet Invoice.Status.PAID - unpaid
    OR pending_verification both count, since only an Admin/SuperAdmin's
    actual approval to PAID should restore access, never the recipient's
    own claim via submit_payment_proof."""
    return (
        Invoice.objects.filter(recipient_user=user, due_date__lt=timezone.localdate())
        .exclude(status=Invoice.Status.PAID)
        .exists()
    )


OVERDUE_INVOICE_MESSAGE = (
    "Your invoice is overdue. Please submit payment or contact your administrator to restore chat access."
)
