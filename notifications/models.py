from django.conf import settings
from django.db import models


class NotificationType(models.TextChoices):
    USAGE_WARNING = "usage_warning", "Usage limit warning"
    PLAN_CHANGE = "plan_change", "Plan changed"
    TRIAL_EXPIRING = "trial_expiring", "Trial expiring soon"
    TRIAL_EXPIRED = "trial_expired", "Trial expired"
    ADMIN_CHANGE = "admin_change", "Admin changed your account"
    MODEL_SYNC_AVAILABLE = "model_sync_available", "New models available to sync"


# One boolean per type, checked as f"email_{notification_type}" - see
# NotificationPreference.wants_email() below. Keeping this list in one
# place means adding a new NotificationType only needs a matching field
# added to NotificationPreference, nothing else has to change.
EMAIL_TOGGLE_LABELS = [
    (NotificationType.USAGE_WARNING, "Usage limit warnings (80%+ of a cap)"),
    (NotificationType.PLAN_CHANGE, "Your plan changes"),
    (NotificationType.TRIAL_EXPIRING, "Trial expiring soon"),
    (NotificationType.TRIAL_EXPIRED, "Trial expired"),
    (NotificationType.ADMIN_CHANGE, "An admin changed your role/limits"),
    (NotificationType.MODEL_SYNC_AVAILABLE, "New AI models are available to sync"),
]


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    notification_type = models.CharField(max_length=30, choices=NotificationType.choices)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    is_read = models.BooleanField(default=False)
    email_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user}: {self.title}"


class NotificationPreference(models.Model):
    """In-app notifications are always created - these fields only gate
    whether an email is ALSO sent for that type. Missing a row (not yet
    created for a user) means "email everything", the safe default."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preference",
    )
    email_usage_warning = models.BooleanField(default=True)
    email_plan_change = models.BooleanField(default=True)
    email_trial_expiring = models.BooleanField(default=True)
    email_trial_expired = models.BooleanField(default=True)
    email_admin_change = models.BooleanField(default=True)
    email_model_sync_available = models.BooleanField(default=True)

    def wants_email(self, notification_type):
        return getattr(self, f"email_{notification_type}", True)

    def __str__(self):
        return f"Notification preferences for {self.user}"
