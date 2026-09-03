from django.contrib import admin

from providers.models import Provider, ProviderModel


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "adapter_type", "is_connected", "last_sync_status", "last_synced_at"]
    # api_key_encrypted/api_key_last4 are set-only via Provider.set_api_key()
    # from the Connect flow - never editable as plain admin form fields,
    # which would defeat the point of encrypting it at rest.
    readonly_fields = ["api_key_last4", "is_connected", "last_synced_at", "last_sync_status", "last_sync_error"]
    exclude = ["api_key_encrypted"]


@admin.register(ProviderModel)
class ProviderModelAdmin(admin.ModelAdmin):
    list_display = ["provider", "model_id", "display_name", "is_enabled", "is_new", "is_retired"]
    list_filter = ["provider", "is_enabled", "is_retired"]
    search_fields = ["model_id", "display_name"]
