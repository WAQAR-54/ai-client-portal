from datetime import timedelta

from celery import shared_task
from django.utils import timezone


@shared_task
def check_for_new_models():
    """Daily discovery pass: looks for provider model IDs not yet tracked
    as a ModelConfig and, if any are found, notifies SuperAdmins - it never
    creates or enables anything itself. Model sync/enable is SuperAdmin-only
    (role hierarchy prompt) - a SuperAdmin still has to visit Models -> Sync
    Models and explicitly choose what to import, same as clicking the
    button manually; this task only saves them from having to remember to
    check."""
    from accounts.models import User
    from chat.model_sync import fetch_all_available_models, known_model_keys
    from notifications.models import NotificationType
    from notifications.notify import notify, recently_notified

    fetched = fetch_all_available_models()
    existing = known_model_keys()
    new_ids = []
    for provider, entry in fetched.items():
        if not entry["configured"] or entry["error"]:
            continue
        for model_id in entry["models"]:
            if (provider, model_id) not in existing:
                new_ids.append(f"{provider}/{model_id}")

    if not new_ids:
        return

    preview = ", ".join(new_ids[:5]) + ("..." if len(new_ids) > 5 else "")
    for admin in User.objects.filter(role=User.Role.SUPERADMIN, is_active=True):
        if recently_notified(admin, NotificationType.MODEL_SYNC_AVAILABLE, since=timezone.now() - timedelta(hours=20)):
            continue
        notify(
            admin,
            NotificationType.MODEL_SYNC_AVAILABLE,
            title="New AI models available to sync",
            body=f"{len(new_ids)} new model(s) found: {preview}. Visit Admin, Models, Sync Models to review.",
        )
