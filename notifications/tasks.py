import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.translation import gettext as _
from django.utils.translation import override as translation_override

logger = logging.getLogger(__name__)

TRIAL_EXPIRING_NOTICE_DAYS = getattr(settings, "TRIAL_EXPIRING_NOTICE_DAYS", 2)

# (accent, accent_soft, content partial) per NotificationType - drives the
# per-type visual in email_generic.html. Keyed by the raw string value
# (matching NotificationType's choices) rather than importing the enum, to
# keep this a plain module-level constant. A type missing here (or a stale
# value from before a type was added) falls back to _DEFAULT_EMAIL_STYLE.
_EMAIL_TYPE_STYLE = {
    "usage_warning": ("#b5761e", "#fcf0dc", "notifications/_email_content_usage_warning.html"),
    "trial_expiring": ("#b5761e", "#fcf0dc", "notifications/_email_content_trial_expiring.html"),
    "trial_expired": ("#c7443f", "#fbe7e8", "notifications/_email_content_trial_expired.html"),
    "plan_change": ("#1e9a6c", "#e3f5ec", "notifications/_email_content_plan_change.html"),
    "model_sync_available": ("#00aef0", "#e3f6fd", "notifications/_email_content_model_sync.html"),
    "account_created": ("#00aef0", "#e3f6fd", "notifications/_email_content_account_created.html"),
}
_DEFAULT_EMAIL_STYLE = ("#00aef0", "#e3f6fd", "notifications/_email_content_default.html")


@shared_task
def send_notification_email(notification_id):
    from notifications.models import Notification

    notification = Notification.objects.select_related("user").filter(id=notification_id).first()
    if not notification or not notification.user.email:
        return

    # notification.title/body were already rendered in the recipient's
    # language at the notify() call site (every call site wraps its own
    # title/body construction in `with translation.override(user.
    # preferred_language):` before calling notify() - see e.g.
    # governance/views.py's _notify_plan_change) - the override here is
    # only for this email's own chrome (button/footer text) around them.
    from notifications.emailing import send_tracked_email

    accent, accent_soft, content_template = _EMAIL_TYPE_STYLE.get(notification.notification_type, _DEFAULT_EMAIL_STYLE)
    site_url = settings.SITE_URL.rstrip("/")
    with translation_override(notification.user.preferred_language):
        html_body = render_to_string(
            "notifications/email_generic.html",
            {
                "notification": notification,
                "accent": accent,
                "accent_soft": accent_soft,
                "content_template": content_template,
                "portal_url": site_url + reverse("chat:chat_home"),
                "preferences_url": site_url + reverse("accounts:profile"),
                "password_reset_url": site_url + reverse("accounts:password_reset_request"),
            },
        )
    sent, error = send_tracked_email(
        to_email=notification.user.email,
        subject=f"[AI Client Portal] {notification.title}",
        text_body=strip_tags(html_body),
        html_body=html_body,
    )
    if not sent:
        logger.warning("Notification email %s to %s failed: %s", notification.id, notification.user.email, error)
        return
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
                with translation_override(assignment.user.preferred_language):
                    title = _("Your trial is ending soon")
                    body = _(
                        "Your %(plan)s trial ends in about %(days)s day(s). "
                        "Contact your administrator if you'd like to keep full access."
                    ) % {"plan": assignment.plan.name, "days": days_left}
                notify(
                    assignment.user,
                    NotificationType.TRIAL_EXPIRING,
                    title=title,
                    body=body,
                    metadata={"days_left": days_left, "plan_name": assignment.plan.name},
                )
                expiring_count += 1
        else:
            if not recently_notified(assignment.user, NotificationType.TRIAL_EXPIRED, since=assignment.assigned_at):
                with translation_override(assignment.user.preferred_language):
                    title = _("Your trial has ended")
                    body = _("Your trial has ended — contact your administrator to continue.")
                notify(
                    assignment.user,
                    NotificationType.TRIAL_EXPIRED,
                    title=title,
                    body=body,
                    metadata={"plan_name": assignment.plan.name},
                )
                expired_count += 1

    logger.info("Trial expiry sweep: %d expiring-soon, %d expired notifications sent", expiring_count, expired_count)
    return {"expiring": expiring_count, "expired": expired_count}
