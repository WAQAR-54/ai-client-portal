"""Single entry point for creating a notification. Always creates the
in-app Notification row; only queues the email if the user's preference
(or the safe default of "yes") allows it for that type."""

from notifications.models import Notification, NotificationPreference


def notify(user, notification_type, title, body="", metadata=None):
    notification = Notification.objects.create(
        user=user,
        notification_type=notification_type,
        title=title,
        body=body,
        metadata=metadata or {},
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


def notification_action_url(notification):
    """Where clicking this notification should go, or None if there's
    nowhere meaningful to send it (the bell dropdown then keeps its
    plain mark-read-only behavior for that one). A plain function, not a
    Notification model method, so it can freely reverse() into billing/
    governance/providers URLs without pulling those apps into
    notifications' own model-import graph. One deliberate destination
    per NotificationType, matching what each type's own body text
    already tells the recipient to go look at."""
    from django.urls import reverse

    from notifications.models import NotificationType

    meta = notification.metadata or {}
    if notification.notification_type in (NotificationType.PLAN_CHANGE,):
        return reverse("billing:my_plans")
    if notification.notification_type in (NotificationType.TRIAL_EXPIRING, NotificationType.TRIAL_EXPIRED):
        return reverse("billing:my_plans")
    if notification.notification_type == NotificationType.INVOICE_PAYMENT_SUBMITTED:
        invoice_id = meta.get("invoice_id")
        return (
            reverse("billing:invoice_detail", kwargs={"invoice_id": invoice_id})
            if invoice_id
            else reverse("billing:invoices")
        )
    if notification.notification_type == NotificationType.REFUND_REQUESTED:
        return reverse("billing:refund_requests")
    if notification.notification_type == NotificationType.PLAN_CANCELLATION:
        return reverse("billing:my_plans")
    if notification.notification_type == NotificationType.REFUND_DECISION:
        invoice_id = meta.get("invoice_id")
        return (
            reverse("billing:invoice_detail", kwargs={"invoice_id": invoice_id})
            if invoice_id
            else reverse("billing:my_invoices")
        )
    if notification.notification_type == NotificationType.ADMIN_CHANGE:
        return reverse("accounts:profile")
    if notification.notification_type == NotificationType.ACCOUNT_CREATED:
        return reverse("accounts:dashboard")
    if notification.notification_type == NotificationType.MODEL_SYNC_AVAILABLE:
        return reverse("providers:list")
    if notification.notification_type == NotificationType.USAGE_WARNING:
        return reverse("chat:chat_home")
    return None
