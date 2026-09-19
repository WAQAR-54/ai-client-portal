from django.contrib import admin

from governance.models import AuditLog, SystemPromptVersion, UsageLimit


@admin.register(SystemPromptVersion)
class SystemPromptVersionAdmin(admin.ModelAdmin):
    list_display = ["department", "tone_preference", "is_active", "created_by", "created_at"]
    list_filter = ["department", "is_active"]


@admin.register(UsageLimit)
class UsageLimitAdmin(admin.ModelAdmin):
    list_display = [
        "user",
        "department",
        "daily_token_cap",
        "monthly_token_cap",
        "session_limit",
        "budget_cap_currency",
    ]
    list_filter = ["department"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["timestamp", "actor", "action_type", "target_type", "target_id"]
    list_filter = ["action_type", "target_type"]
    search_fields = ["actor__email", "target_id"]
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Without this, a Django-admin superuser could still delete audit
        # rows via the admin UI's "Delete selected" action/object page -
        # has_add_permission/has_change_permission alone don't cover
        # delete. The model's own delete() override (governance/models.py)
        # would raise anyway if this somehow got bypassed, but blocking it
        # here means the delete option/confirmation page never even shows.
        return False
