import uuid

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


def _fernet():
    """Same FIELD_ENCRYPTION_KEY-based scheme as providers/models.py::
    Provider.set_api_key - kept as its own copy here rather than a shared
    import, matching this codebase's existing per-app-isolation for
    small encryption helpers."""
    key = settings.FIELD_ENCRYPTION_KEY
    return Fernet(key.encode() if isinstance(key, str) else key)


class NotificationType(models.TextChoices):
    USAGE_WARNING = "usage_warning", "Usage limit warning"
    PLAN_CHANGE = "plan_change", "Plan changed"
    TRIAL_EXPIRING = "trial_expiring", "Trial expiring soon"
    TRIAL_EXPIRED = "trial_expired", "Trial expired"
    ADMIN_CHANGE = "admin_change", "Admin changed your account"
    MODEL_SYNC_AVAILABLE = "model_sync_available", "New models available to sync"
    ACCOUNT_CREATED = "account_created", "Account created"


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
    (NotificationType.ACCOUNT_CREATED, "Your account was created"),
]


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    notification_type = models.CharField(max_length=30, choices=NotificationType.choices)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    # Optional structured data a notify() call site can pass alongside the
    # plain title/body (e.g. {"plan_name": ...}, {"days_left": ...}) so the
    # per-type email template (see notifications/tasks.py's
    # _EMAIL_TYPE_STYLE) can render a richer visual than plain text - every
    # content template treats every key as optional and falls back to
    # title/body when absent, so this never has to be filled in.
    metadata = models.JSONField(default=dict, blank=True)
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
    email_account_created = models.BooleanField(default=True)

    def wants_email(self, notification_type):
        return getattr(self, f"email_{notification_type}", True)

    def __str__(self):
        return f"Notification preferences for {self.user}"


class EmailSettings(models.Model):
    """Singleton (always pk=1, via .load()) - the admin-configurable SMTP
    connection every real email in the app sends through (see
    notifications/emailing.py::send_tracked_email), replacing the old
    settings.py/env-var EMAIL_* configuration so it can be changed and
    tested from the admin console without a redeploy. password_encrypted
    follows the same Fernet scheme as Provider.set_api_key - never exposed
    outside get_password(), which only the sending code calls."""

    host = models.CharField(max_length=255, blank=True)
    port = models.PositiveIntegerField(default=587)

    class Encryption(models.TextChoices):
        TLS = "tls", "TLS"
        SSL = "ssl", "SSL"

    encryption = models.CharField(max_length=5, choices=Encryption.choices, default=Encryption.TLS)
    username = models.CharField(max_length=255, blank=True)
    password_encrypted = models.BinaryField(blank=True, default=b"")
    from_address = models.EmailField(blank=True)

    class Status(models.TextChoices):
        UNVERIFIED = "unverified", "Saved — not tested yet"
        CONNECTED = "connected", "Verified working"
        FAILED = "failed", "Connection failed"
        STALE = "stale", "Config changed — retest needed"

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.UNVERIFIED)
    last_test_error = models.TextField(blank=True)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Email settings"
        verbose_name_plural = "Email settings"

    def __str__(self):
        return f"Email settings ({self.get_status_display()})"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def set_password(self, raw_password):
        self.password_encrypted = _fernet().encrypt(raw_password.encode()) if raw_password else b""

    def get_password(self):
        if not self.password_encrypted:
            return ""
        try:
            return _fernet().decrypt(bytes(self.password_encrypted)).decode()
        except InvalidToken:
            # FIELD_ENCRYPTION_KEY changed since this was saved - treat as
            # "no usable password" rather than raising and breaking every
            # email send.
            return ""

    def is_configured(self):
        return bool(self.host and self.username)


class EmailLog(models.Model):
    """One row per email the app has actually tried to send (see
    notifications/emailing.py::send_tracked_email) - the admin Email Logs
    page reads this directly. opened_at is set by notifications/views.py::
    track_email_open the first time the tracking pixel embedded in an
    HTML email is fetched; a plain-text-only email (no html_body passed)
    never gets a pixel and opened_at stays permanently null for it."""

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    recipient = models.EmailField()
    subject = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices)
    error_message = models.TextField(blank=True)
    tracking_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    opened_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_status_display()} to {self.recipient}: {self.subject}"
