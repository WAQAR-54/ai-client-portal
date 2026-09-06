from itertools import groupby

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from governance.features import RequireStandaloneToolAccessMixin, require_standalone_tool_access
from playground.models import PlaygroundRun

# "12 / 20 runs left today" in the reference mockup - a real per-user daily
# cap, not decorative, since the admin Dashboard's usage stats and this
# quota are backed by the same PlaygroundRun log.
DAILY_RUN_LIMIT = getattr(settings, "PLAYGROUND_DAILY_RUN_LIMIT", 20)

# Tier -> the reference mockup's "Fast/Balanced/Powerful" speed pill.
_TIER_SPEED_LABEL = {
    "economy": ("Fast", "fast"),
    "default": ("Balanced", "balanced"),
    "premium": ("Powerful", "powerful"),
}


def _runs_today_count(user):
    today = timezone.localdate()
    return PlaygroundRun.objects.filter(user=user, created_at__date=today).count()


class PlaygroundView(LoginRequiredMixin, RequireStandaloneToolAccessMixin, TemplateView):
    """Standalone Code Playground page - deliberately NOT linked from the
    main sidebar nav (see base.html): reached only via its direct URL,
    shared out-of-band. Off by default for User/Manager (see
    governance.features.user_can_access_standalone_tool); Admin/SuperAdmin
    always has it so they can check it themselves, per the admin
    Dashboard's own link to it."""

    template_name = "playground/playground.html"
    feature_key = "code_playground"

    def handle_no_permission(self):
        """This link is shared standalone (see the class docstring), so
        whoever opens it while logged out just lands on a bare login form
        with no context - a one-line reason why they're there."""
        messages.info(self.request, _("Kindly log in to use Code Playground."))
        return super().handle_no_permission()

    def get_context_data(self, **kwargs):
        from providers.models import ProviderModel

        models_qs = (
            ProviderModel.objects.filter(is_enabled=True, is_playground_enabled=True)
            .select_related("provider")
            .order_by("provider__name", "tier")
        )
        provider_groups = [
            {
                "provider_name": provider_name,
                "models": [
                    {
                        "id": m.id,
                        "label": m.display_label,
                        "provider_slug": m.provider.slug,
                        "speed_label": _TIER_SPEED_LABEL.get(m.tier, ("Balanced", "balanced"))[0],
                        "speed_class": _TIER_SPEED_LABEL.get(m.tier, ("Balanced", "balanced"))[1],
                    }
                    for m in models
                ],
            }
            for provider_name, models in groupby(models_qs, key=lambda m: m.provider.name)
        ]

        runs_used = _runs_today_count(self.request.user)
        return super().get_context_data(**kwargs) | {
            "provider_groups": provider_groups,
            "daily_limit": DAILY_RUN_LIMIT,
            "runs_used_today": runs_used,
            "runs_remaining": max(0, DAILY_RUN_LIMIT - runs_used),
        }


@login_required
@require_standalone_tool_access("code_playground")
@require_POST
def log_run(request):
    """Hit by the Run button's JS on every click - logs real usage (for the
    quota pill and the admin Dashboard's stats) even though the code
    execution shown afterward is simulated client-side, not a real
    sandbox."""
    runs_used = _runs_today_count(request.user)
    if runs_used >= DAILY_RUN_LIMIT:
        return JsonResponse({"allowed": False, "remaining": 0}, status=429)

    language = request.POST.get("language", PlaygroundRun.Language.PYTHON)
    if language not in PlaygroundRun.Language.values:
        language = PlaygroundRun.Language.PYTHON
    PlaygroundRun.objects.create(user=request.user, language=language)

    remaining = max(0, DAILY_RUN_LIMIT - (runs_used + 1))
    return JsonResponse({"allowed": True, "remaining": remaining})
