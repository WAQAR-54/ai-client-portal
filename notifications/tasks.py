import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.translation import override as translation_override

logger = logging.getLogger(__name__)

TRIAL_EXPIRING_NOTICE_DAYS = getattr(settings, "TRIAL_EXPIRING_NOTICE_DAYS", 2)


@shared_task
def send_notification_email(notification_id):
    from notifications.models import Notification

    notification = Notification.objects.select_related("user").filter(id=notification_id).first()
    if not notification or not notification.user.email:
        return

    # Only the email's own chrome (button/footer text) follows the
    # recipient's language preference - notification.title/body are
    # written in English at the many notify() call sites throughout the
    # codebase and aren't translated in this pass (see AI_Client_Portal
    # notes on B5 scope).
    with translation_override(notification.user.preferred_language):
        html_body = render_to_string("notifications/email_generic.html", {"notification": notification})
    send_mail(
        subject=f"[AI Client Portal] {notification.title}",
        message=strip_tags(html_body),
        html_message=html_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[notification.user.email],
        fail_silently=False,
    )
    notification.email_sent = True
    notification.save(update_fields=["email_sent"])
    logger.info("Sent notification email %s to %s", notification.id, notification.user.email)


@shared_task
def sweep_expiring_demo_plans():
    """Daily beat task: notify users whose demo plan is about to expire or
    has just expired. Blocking access itself does NOT depend on this task
    running (governance/plans.py checks expiry live on every request) —
    this only handles the proactive email/in-app heads-up, which does need
    a scheduler."""
    from notifications.models import NotificationType
    from notifications.notify import notify, recently_notified
    from governance.models import UserPlanAssignment

    now = timezone.now()
    notice_cutoff = now + timedelta(days=TRIAL_EXPIRING_NOTICE_DAYS)

    assignments = UserPlanAssignment.objects.select_related("user", "plan").filter(
        plan__is_demo=True, expires_at__isnull=False
    )

    expiring_count = expired_count = 0
    for assignment in assignments:
        if assignment.expires_at > now:
            if assignment.expires_at <= notice_cutoff and not recently_notified(
                assignment.user,
                NotificationType.TRIAL_EXPIRING,
                since=assignment.assigned_at,
            ):
                days_left = max(1, (assignment.expires_at - now).days)
                notify(
                    assignment.user,
                    NotificationType.TRIAL_EXPIRING,
                    title="Your trial is ending soon",
                    body=f"Your {assignment.plan.name} trial ends in about {days_left} day(s). "
                    "Contact your administrator if you'd like to keep full access.",
                )
                expiring_count += 1
        else:
            if not recently_notified(assignment.user, NotificationType.TRIAL_EXPIRED, since=assignment.assigned_at):
                notify(
                    assignment.user,
                    NotificationType.TRIAL_EXPIRED,
                    title="Your trial has ended",
                    body="Your trial has ended — contact your administrator to continue.",
                )
                expired_count += 1

    logger.info("Trial expiry sweep: %d expiring-soon, %d expired notifications sent", expiring_count, expired_count)
    return {"expiring": expiring_count, "expired": expired_count}
