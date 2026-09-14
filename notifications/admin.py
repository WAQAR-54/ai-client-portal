from django.contrib import admin

from notifications.models import EmailLog, EmailSettings, Notification, NotificationPreference


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Read-only, like AuditLog (governance/admin.py) - a log of what was
    actually sent, not something to hand-edit. Gives a SuperAdmin a way
    to see/search a user's full notification history that the app's own
    UI (the bell's last-10, or one user's own /notifications/ page)
    can't - e.g. "did this user ever get notified about X"."""

    list_display = ["user", "notification_type", "title", "is_read", "email_sent", "created_at"]
    list_filter = ["notification_type", "is_read", "email_sent"]
    search_fields = ["user__email", "title", "body"]
    readonly_fields = [f.name for f in Notification._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ["user"]
    search_fields = ["user__email"]


@admin.register(EmailSettings)
class EmailSettingsAdmin(admin.ModelAdmin):
    list_display = ["host", "port", "encryption", "status", "last_tested_at"]

    def has_add_permission(self, request):
        # Singleton (EmailSettings.load(), always pk=1) - same reasoning
        # as governance's SiteBranding/SecuritySettings not being
        # separately admin-addable; the real editing surface is the
        # in-app Email Configuration modal (Email Logs page), not this.
        return not EmailSettings.objects.exists()


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    """Read-only - see NotificationAdmin's own reasoning. The in-app
    Email Logs page (governance:email_logs) is the real browsing UI for
    this; this is just so it's reachable from /admin/ too."""

    list_display = ["recipient", "subject", "status", "opened_at", "created_at"]
    list_filter = ["status"]
    search_fields = ["recipient", "subject"]
    readonly_fields = [f.name for f in EmailLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
