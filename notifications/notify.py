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
    if notification.notification_type == NotificationType.NEW_TRUSTED_DEVICE:
        return reverse("accounts:profile") + "#security"
    if notification.notification_type == NotificationType.SYSTEM_ALERT:
        return reverse("governance:dashboard") + "#sys-status-title"
    # MAINTENANCE has no universal destination - governance:maintenance is SuperAdmin-only, and
    # most recipients of a maintenance notice have no page to send them to about it.
    return None


class NotificationCategory:
    """Display-only grouping over the existing NotificationType values (Notification Center's
    optional category filter) - never stored on the model, never a new NotificationType. A type
    left out of _TYPE_CATEGORY (ACCOUNT_CREATED) simply has no category chip; it still shows
    under "All"."""

    SECURITY = "security"
    BILLING = "billing"
    AI_SYSTEM = "ai_system"
    MAINTENANCE = "maintenance"


NOTIFICATION_CATEGORIES = [
    (NotificationCategory.SECURITY, "Security"),
    (NotificationCategory.BILLING, "Billing"),
    (NotificationCategory.AI_SYSTEM, "AI & System"),
    (NotificationCategory.MAINTENANCE, "Maintenance"),
]

_TYPE_CATEGORY = {
    "admin_change": NotificationCategory.SECURITY,
    "new_trusted_device": NotificationCategory.SECURITY,
    "plan_change": NotificationCategory.BILLING,
    "trial_expiring": NotificationCategory.BILLING,
    "trial_expired": NotificationCategory.BILLING,
    "invoice_payment_submitted": NotificationCategory.BILLING,
    "refund_requested": NotificationCategory.BILLING,
    "refund_decision": NotificationCategory.BILLING,
    "plan_cancellation": NotificationCategory.BILLING,
    "model_sync_available": NotificationCategory.AI_SYSTEM,
    "usage_warning": NotificationCategory.AI_SYSTEM,
    "maintenance": NotificationCategory.MAINTENANCE,
    "system_alert": NotificationCategory.AI_SYSTEM,
}

# One icon "kind" per type, reused by both the category grouping above (where a type has a
# category, its icon matches) and the ones that don't (account_created falls back to "generic").
_TYPE_ICON_KIND = dict(_TYPE_CATEGORY, account_created="generic")


def notification_category(notification_type):
    return _TYPE_CATEGORY.get(notification_type)


def notification_types_for_category(category_key):
    return [t for t, c in _TYPE_CATEGORY.items() if c == category_key]


def notification_icon_kind(notification_type):
    return _TYPE_ICON_KIND.get(notification_type, "generic")
