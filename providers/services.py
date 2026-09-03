"""Sync logic - the one place that turns an adapter's fetch_models() result
into ProviderModel rows. Two callers: the Connect flow (governance views,
synchronous - the admin is already waiting on the modal) and the Celery
Beat periodic task (providers/tasks.py)."""

from django.utils import timezone

from providers.adapters import ProviderAPIError
from providers.adapters.sanitize import sanitize_error
from providers.models import Provider, ProviderModel


def sync_provider(provider: Provider) -> dict:
    """Fetches the provider's current model list and reconciles it against
    ProviderModel rows already on file. Returns {"success": bool,
    "new_count": int, "updated_count": int, "retired_count": int,
    "error": str|None}.

    Guardrails, both deliberate and both load-bearing:
    - A newly discovered model is NEVER auto-enabled (is_enabled=False,
      is_new=True) - cost control. Only an explicit admin toggle turns
      one on.
    - A model an already-connected provider stops listing is marked
      is_retired=True and forced is_enabled=False, but never deleted -
      Plan.allowed_models / Message.model_used references must keep
      resolving for historical data.
    """
    api_key = provider.get_decrypted_key()
    if not api_key:
        result = {"success": False, "new_count": 0, "updated_count": 0, "retired_count": 0, "error": "Not connected"}
        _record_sync_result(provider, result)
        return result

    try:
        fetched = provider.get_adapter().fetch_models(api_key)
    except ProviderAPIError as exc:
        result = {
            "success": False,
            "new_count": 0,
            "updated_count": 0,
            "retired_count": 0,
            "error": sanitize_error(str(exc), api_key),
        }
        _record_sync_result(provider, result)
        return result
    except Exception as exc:  # noqa: BLE001 - an adapter bug shouldn't take the whole sync loop down
        result = {
            "success": False,
            "new_count": 0,
            "updated_count": 0,
            "retired_count": 0,
            "error": sanitize_error(str(exc), api_key),
        }
        _record_sync_result(provider, result)
        return result

    seen_model_ids = set()
    new_count = 0
    updated_count = 0
    for entry in fetched:
        model_id = entry["model_id"]
        seen_model_ids.add(model_id)
        obj, created = ProviderModel.objects.get_or_create(
            provider=provider,
            model_id=model_id,
            defaults={
                "display_name": entry.get("display_name", "") or model_id,
                "input_price_per_mtok": entry.get("input_price"),
                "output_price_per_mtok": entry.get("output_price"),
                # is_enabled deliberately omitted - the field default
                # (False) applies, and is never passed explicitly here so
                # a future refactor can't accidentally start honoring a
                # True value from an adapter.
            },
        )
        if created:
            new_count += 1
        else:
            # Existing rows: refresh display/pricing and un-retire if the
            # provider is listing it again, but is_enabled is untouched -
            # a sync only ever discovers/retires, it never re-enables.
            obj.display_name = entry.get("display_name", "") or model_id
            obj.input_price_per_mtok = entry.get("input_price")
            obj.output_price_per_mtok = entry.get("output_price")
            obj.is_retired = False
            obj.save(update_fields=["display_name", "input_price_per_mtok", "output_price_per_mtok", "is_retired"])
            updated_count += 1

    retired_qs = provider.models.exclude(model_id__in=seen_model_ids).filter(is_retired=False)
    retired_count = retired_qs.count()
    retired_qs.update(is_retired=True, is_enabled=False)

    result = {
        "success": True,
        "new_count": new_count,
        "updated_count": updated_count,
        "retired_count": retired_count,
        "error": None,
    }
    _record_sync_result(provider, result)
    return result


def _record_sync_result(provider: Provider, result: dict) -> None:
    provider.last_synced_at = timezone.now()
    provider.last_sync_status = Provider.SyncStatus.SUCCESS if result["success"] else Provider.SyncStatus.FAILED
    provider.last_sync_error = result["error"] or ""
    provider.save(update_fields=["last_synced_at", "last_sync_status", "last_sync_error"])
