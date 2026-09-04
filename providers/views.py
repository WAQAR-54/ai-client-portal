"""Admin views for the Providers page: connect (paste + verify an API key),
resync (re-fetch that provider's model list), disconnect (clear the key),
toggle-model (enable/disable one already-discovered ProviderModel), and the
list page itself. SuperAdmin-only, same as the legacy Models page (role
hierarchy prompt, Section 1) - an org-wide credential/model registry isn't a
department-scoped Admin's call to make.
"""

from django.contrib import messages as django_messages
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView

from accounts.models import User
from accounts.permissions import SuperAdminRequiredMixin, role_required
from governance.audit import log_action
from providers.models import Provider, ProviderModel
from providers.services import sync_provider


def _provider_row(provider):
    """One provider plus its models, ordered so newly-discovered/pending-
    review ones surface first - the thing an admin who just hit Resync
    actually came here to look at."""
    models = list(provider.models.order_by("-is_new", "is_retired", "model_id"))
    return {
        "provider": provider,
        "models": models,
        "new_count": sum(1 for m in models if m.is_new and not m.is_retired),
        "enabled_count": sum(1 for m in models if m.is_enabled),
    }


def _all_provider_rows():
    return [_provider_row(p) for p in Provider.objects.order_by("name")]


class ProviderListView(SuperAdminRequiredMixin, TemplateView):
    template_name = "providers/list.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {"provider_rows": _all_provider_rows()}


def _sync_result_message(request, provider, result):
    if not result["success"]:
        django_messages.error(
            request,
            _("%(provider)s: sync failed — %(error)s") % {"provider": provider.name, "error": result["error"]},
        )
        return
    django_messages.success(
        request,
        _(
            "%(provider)s: %(new)s new model(s) found (disabled by default), "
            "%(updated)s refreshed, %(retired)s retired."
        )
        % {
            "provider": provider.name,
            "new": result["new_count"],
            "updated": result["updated_count"],
            "retired": result["retired_count"],
        },
    )


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def connect_provider(request, provider_id):
    """Verifies the pasted key against the provider's own API BEFORE saving
    anything - a bad key must never get stored as "connected". On success,
    immediately runs a first sync so the admin sees real models right away
    instead of an empty card. The raw key itself is never logged or shown
    back - only api_key_last4 (see Provider.set_api_key)."""
    provider = get_object_or_404(Provider, id=provider_id)
    raw_key = request.POST.get("api_key", "").strip()
    if not raw_key:
        return HttpResponseBadRequest("API key is required")

    if not provider.get_adapter().test_connection(raw_key):
        django_messages.error(
            request,
            _("Couldn't connect to %(provider)s — check the API key and try again.") % {"provider": provider.name},
        )
        return redirect("providers:list")

    provider.set_api_key(raw_key)
    provider.is_connected = True
    provider.save(update_fields=["api_key_encrypted", "api_key_last4", "is_connected"])
    log_action(request.user, "provider.connect", provider, new_value=f"key ending {provider.api_key_last4}")

    result = sync_provider(provider)
    _sync_result_message(request, provider, result)
    return redirect("providers:list")


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def resync_provider(request, provider_id):
    provider = get_object_or_404(Provider, id=provider_id)
    if not provider.is_connected:
        django_messages.error(request, _("%(provider)s isn't connected yet.") % {"provider": provider.name})
    else:
        result = sync_provider(provider)
        log_action(
            request.user,
            "provider.resync",
            provider,
            new_value=f"new={result['new_count']} updated={result['updated_count']} retired={result['retired_count']}",
        )
        _sync_result_message(request, provider, result)

    if request.headers.get("HX-Request"):
        return render(request, "providers/_provider_card.html", _provider_row(provider))
    return redirect("providers:list")


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def disconnect_provider(request, provider_id):
    """Clears the stored key and force-disables every currently-enabled
    model from this provider - a disconnected provider has no working key,
    so leaving its models enabled would just mean requests silently fail
    over to the next candidate (or fail outright) instead of the admin
    console reflecting reality. ProviderModel rows themselves are kept
    (never deleted), same reasoning as a retired model - Plan/Message
    history may still reference them."""
    provider = get_object_or_404(Provider, id=provider_id)
    disabled_count = provider.models.filter(is_enabled=True).update(is_enabled=False)
    provider.api_key_encrypted = b""
    provider.api_key_last4 = ""
    provider.is_connected = False
    provider.save(update_fields=["api_key_encrypted", "api_key_last4", "is_connected"])
    log_action(request.user, "provider.disconnect", provider, old_value=f"{disabled_count} model(s) disabled")
    if disabled_count:
        django_messages.success(
            request,
            ngettext(
                "%(provider)s disconnected — %(count)s model was disabled with it.",
                "%(provider)s disconnected — %(count)s models were disabled with it.",
                disabled_count,
            )
            % {"provider": provider.name, "count": disabled_count},
        )
    else:
        django_messages.success(request, _("%(provider)s disconnected.") % {"provider": provider.name})

    if request.headers.get("HX-Request"):
        return render(request, "providers/_provider_card.html", _provider_row(provider))
    return redirect("providers:list")


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def toggle_provider_model(request, model_id):
    """Flips is_enabled and marks the model reviewed (is_new=False) -
    reachable from the Providers page's "N new models pending review" badge,
    so an explicit toggle in either direction is what clears it, not just
    viewing the page."""
    provider_model = get_object_or_404(ProviderModel, id=model_id)
    old_value = provider_model.is_enabled
    provider_model.is_enabled = not provider_model.is_enabled
    provider_model.is_new = False
    provider_model.save(update_fields=["is_enabled", "is_new"])
    log_action(
        request.user,
        "providermodel.enable" if provider_model.is_enabled else "providermodel.disable",
        provider_model,
        old_value=old_value,
        new_value=provider_model.is_enabled,
    )

    if request.headers.get("HX-Request"):
        return render(request, "providers/_provider_card.html", _provider_row(provider_model.provider))
    return redirect("providers:list")
