import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=600, max_retries=3)
def sweep_conversation_retention():
    """Deletes whole Conversations (cascading to their Messages) once
    they're older than their owner's Department.retention_period - see
    accounts/models.py::Department.retention_days. A department with no
    retention limit (retention_days is None, i.e. "Forever") is skipped
    entirely. Measured from Conversation.updated_at (last activity), not
    created_at, so a conversation someone keeps coming back to is never
    swept just because it's old.

    Retries up to 3 times with backoff on an unexpected failure (a DB
    blip mid-sweep) - safe to retry since each department's delete is
    keyed off the same cutoff and re-running just re-evaluates it;
    already-deleted rows simply won't match a second time."""
    from accounts.models import Department
    from chat.models import Conversation

    total_deleted = 0
    for department in Department.objects.exclude(retention_period=Department.RetentionPeriod.FOREVER):
        cutoff = timezone.now() - timezone.timedelta(days=department.retention_days)
        queryset = Conversation.all_objects.filter(user__department=department, updated_at__lt=cutoff)
        count = queryset.count()
        if count:
            queryset.delete()
            total_deleted += count
            logger.info(
                "Retention sweep: deleted %s conversation(s) for department %s (older than %s days)",
                count,
                department.name,
                department.retention_days,
            )
    return total_deleted


@shared_task(autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=300, max_retries=3)
def send_maintenance_notice(window_id, kind):
    """Emails one kind of maintenance notice (scheduled / started / completed / cancelled) to the active users through
    the normal notification path (and so the global email shell). Safe to run twice: the notice is claimed atomically
    (governance/maintenance.py::deliver_notice), so a retry or a second worker never sends it again."""
    from governance.maintenance import deliver_notice

    return deliver_notice(window_id, kind)


@shared_task
def advance_maintenance():
    """Every minute (beat): applies a maintenance window's due start or end so the audit row and the emails appear on
    time even when nobody is visiting. The site itself never depends on this - the request middleware applies the same
    transitions by the clock (governance/maintenance.py::current_state)."""
    from governance.maintenance import advance, invalidate

    advance()
    invalidate()
