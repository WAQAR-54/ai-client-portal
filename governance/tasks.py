import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def sweep_conversation_retention():
    """Deletes whole Conversations (cascading to their Messages) once
    they're older than their owner's Department.retention_period - see
    accounts/models.py::Department.retention_days. A department with no
    retention limit (retention_days is None, i.e. "Forever") is skipped
    entirely. Measured from Conversation.updated_at (last activity), not
    created_at, so a conversation someone keeps coming back to is never
    swept just because it's old."""
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
