from datetime import timedelta

from celery import shared_task
from django.utils import timezone


@shared_task
def sync_all_connected_providers():
    """Periodic beat task: re-syncs every connected Provider's model list
    via sync_provider() (still bound by its own never-auto-enable
    guardrail - this only discovers/retires ProviderModel rows, exactly
    what a manual Resync click does) and notifies SuperAdmins if any new
    models were found - a SuperAdmin still has to visit the Providers page
    and explicitly enable anything found; this only saves them from having
    to remember to click Resync themselves."""
    from accounts.models import User
    from notifications.models import NotificationType
    from notifications.notify import notify, recently_notified
    from providers.models import Provider
    from providers.services import sync_provider

    new_by_provider = {}
    for provider in Provider.objects.filter(is_connected=True):
        result = sync_provider(provider)
        if result["success"] and result["new_count"]:
            new_by_provider[provider.name] = result["new_count"]

    if not new_by_provider:
        return

    total_new = sum(new_by_provider.values())
    preview = ", ".join(f"{name} ({count})" for name, count in new_by_provider.items())
    for admin in User.objects.filter(role=User.Role.SUPERADMIN, is_active=True):
        if recently_notified(admin, NotificationType.MODEL_SYNC_AVAILABLE, since=timezone.now() - timedelta(hours=20)):
            continue
        notify(
            admin,
            NotificationType.MODEL_SYNC_AVAILABLE,
            title="New AI models available to sync",
            body=f"{total_new} new model(s) found: {preview}. Visit Admin, Providers to review.",
        )
