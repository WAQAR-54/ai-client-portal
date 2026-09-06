import json
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
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

from domaingen.models import DomainSearch
from domaingen.whois import check_domain_available
from governance.features import RequireStandaloneToolAccessMixin, require_standalone_tool_access

# A real per-user daily cap on AI generation calls - same cost-control
# philosophy as playground.views.DAILY_RUN_LIMIT, just for a feature that
# actually spends real provider tokens on every "Generate" click.
DAILY_SEARCH_LIMIT = getattr(settings, "DOMAIN_GENERATOR_DAILY_LIMIT", 20)

# The only TLDs domaingen.whois.check_domain_available can actually verify -
# matches the reference UI's own TLD chip row (All/.com/.ai/.io) exactly,
# so nothing is offered that the backend can't really check.
ALLOWED_TLDS = ["com", "ai", "io"]

# Typical street price for a first-year registration - not webhoster.pk's
# actual live pricing (no reseller API/credentials for that), so this is
# labeled "around" in the UI rather than presented as an exact quote.
_TLD_PRICE_ESTIMATE = {"com": Decimal("12"), "io": Decimal("35"), "ai": Decimal("70")}

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,22}[a-z0-9]$")

_SYSTEM_PROMPT = """You are a domain name brainstorming assistant. Given a short description of a \
business idea, suggest exactly {count} distinct, brandable domain names for it.

Respond with ONLY a JSON array (no markdown fences, no prose, no explanation) of exactly {count} \
objects, each shaped like:
{{"name": "shortlowercasename", "tld": "com"}}

Rules:
- "name" must be lowercase letters, digits, and hyphens only, 3-20 characters, no spaces.
- "tld" must be one of: {tlds}.
- Names must be genuinely different from each other, memorable, and relevant to the idea.
- Do not include the TLD in "name".
- Output raw JSON only - your entire response must be parseable by json.loads()."""


SUGGESTION_COUNT = 5


def _today_search_count(user):
    today = timezone.localdate()
    return DomainSearch.objects.filter(user=user, created_at__date=today).count()


def _complete_with_usage(provider, prompt, model_name):
    """Fully consumes stream_chat (rather than calling the plain complete()
    method) purely to get real input/output token counts back for honest
    cost accounting - the UI doesn't stream this response, it's used once
    the full JSON array is in hand."""
    text_parts = []
    input_tokens = output_tokens = 0
    for chunk in provider.stream_chat([{"role": "user", "content": prompt}], model_name):
        if chunk.text:
            text_parts.append(chunk.text)
        if chunk.done:
            input_tokens = chunk.input_tokens or 0
            output_tokens = chunk.output_tokens or 0
    return "".join(text_parts), input_tokens, output_tokens


def _parse_suggestions(raw_text, tld_filter):
    """Best-effort JSON parse of the model's response - strips a stray
    markdown fence if the model added one despite instructions, validates
    each item, and silently drops anything malformed rather than failing
    the whole request over one bad entry."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(parsed, list):
        return []

    allowed_tlds = ALLOWED_TLDS if tld_filter == "all" else [tld_filter]
    suggestions = []
    seen = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()
        tld = str(item.get("tld", "")).strip().lower().lstrip(".")
        if not _NAME_RE.match(name) or tld not in allowed_tlds:
            continue
        domain = f"{name}.{tld}"
        if domain in seen:
            continue
        seen.add(domain)
        suggestions.append({"name": name, "tld": tld, "domain": domain})
    return suggestions


class DomainGeneratorView(LoginRequiredMixin, RequireStandaloneToolAccessMixin, TemplateView):
    """Standalone Domain Generator page - same "direct link only, never in
    the sidebar" treatment as Code Playground (see playground.views.
    PlaygroundView), including the admin-always-has-it access rule and
    off-by-default role toggle."""

    template_name = "domaingen/domain_generator.html"
    feature_key = "domain_generator"

    def handle_no_permission(self):
        messages.info(self.request, _("Kindly log in to use Domain Generator."))
        return super().handle_no_permission()

    def get_context_data(self, **kwargs):
        from providers.models import ProviderModel

        models_qs = (
            ProviderModel.objects.filter(is_enabled=True, is_domain_generator_enabled=True)
            .select_related("provider")
            .order_by("provider__name", "tier")
        )
        provider_groups = [
            {
                "provider_name": provider_name,
                "models": [{"id": m.id, "label": m.display_label, "provider_slug": m.provider.slug} for m in models],
            }
            for provider_name, models in groupby(models_qs, key=lambda m: m.provider.name)
        ]
        return super().get_context_data(**kwargs) | {"provider_groups": provider_groups}


@login_required
@require_standalone_tool_access("domain_generator")
@require_POST
def generate_domains(request):
    from chat.providers import ProviderError, get_provider
    from providers.models import ProviderModel

    searches_used = _today_search_count(request.user)
    if searches_used >= DAILY_SEARCH_LIMIT:
        return JsonResponse({"error": "Daily search limit reached. Try again tomorrow."}, status=429)

    query = request.POST.get("query", "").strip()
    if len(query) < 3:
        return JsonResponse({"error": "Describe your idea in a few more words."}, status=400)

    tld_filter = request.POST.get("tld", "all").strip().lower()
    if tld_filter != "all" and tld_filter not in ALLOWED_TLDS:
        tld_filter = "all"

    model_id = request.POST.get("provider_model_id")
    provider_model = (
        ProviderModel.objects.filter(id=model_id, is_enabled=True, is_domain_generator_enabled=True)
        .select_related("provider")
        .first()
    )
    if provider_model is None:
        return JsonResponse({"error": "Pick a model first."}, status=400)

    prompt = _SYSTEM_PROMPT.format(
        count=SUGGESTION_COUNT, tlds=", ".join(ALLOWED_TLDS if tld_filter == "all" else [tld_filter])
    )
    prompt += f"\n\nBusiness idea: {query}"

    provider = get_provider(provider_model.provider)
    try:
        raw_text, input_tokens, output_tokens = _complete_with_usage(provider, prompt, provider_model.model_id)
    except ProviderError as exc:
        return JsonResponse({"error": f"Generation failed: {exc}"}, status=502)

    suggestions = _parse_suggestions(raw_text, tld_filter)
    if not suggestions:
        return JsonResponse({"error": "Couldn't generate suggestions for that idea - try rephrasing it."}, status=502)

    with ThreadPoolExecutor(max_workers=len(suggestions)) as pool:
        availability = list(pool.map(lambda s: check_domain_available(s["name"], s["tld"]), suggestions))

    results = []
    for suggestion, available in zip(suggestions, availability):
        price = _TLD_PRICE_ESTIMATE.get(suggestion["tld"])
        results.append(
            {
                "domain": suggestion["domain"],
                "available": available,
                "price_estimate": f"~${price}/yr" if available and price is not None else None,
            }
        )

    estimated_cost = provider_model.estimate_cost(input_tokens, output_tokens)
    search = DomainSearch.objects.create(
        user=request.user,
        query=query[:300],
        provider_model=provider_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=estimated_cost,
    )

    return JsonResponse(
        {
            "results": results,
            "remaining": max(0, DAILY_SEARCH_LIMIT - (searches_used + 1)),
            "cost_display": f"${estimated_cost:.4f}" if estimated_cost is not None else None,
            "search_id": search.id,
        }
    )
