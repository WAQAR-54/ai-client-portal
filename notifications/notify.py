"""Single entry point for creating a notification. Always creates the
in-app Notification row; only queues the email if the user's preference
(or the safe default of "yes") allows it for that type."""

from notifications.models import Notification, NotificationPreference


def notify(user, notification_type, title, body=""):
    notification = Notification.objects.create(
        user=user,
        notification_type=notification_type,
        title=title,
        body=body,
    )

    preference = NotificationPreference.objects.filter(user=user).first()
    should_email = preference.wants_email(notification_type) if preference else True

    if should_email and user.email:
        from notifications.tasks import send_notification_email

        send_notification_email.delay(notification.id)

    return notification


def recently_notified(user, notification_type, since):
    """Dedup helper - has `user` already gotten this type of notification
    since `since` (a datetime)? Used to avoid re-sending a usage-warning
    email on every single message once a user is already over 80%, and to
    avoid re-sending the same trial-expiring notice every time the daily
    sweep task runs."""
    return Notification.objects.filter(
        user=user,
        notification_type=notification_type,
        created_at__gte=since,
    ).exists()
