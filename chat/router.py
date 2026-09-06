"""Smart routing: classify request complexity, then pick the cheapest
enabled model at that tier the requesting user is permitted to use.
"""

import re

from django.db.models import F

from chat.prompts import ROUTER_CLASSIFICATION_PROMPT
from chat.providers import ProviderError, get_provider
from providers.models import ProviderModel

TIER_ORDER = [ProviderModel.Tier.ECONOMY, ProviderModel.Tier.DEFAULT, ProviderModel.Tier.PREMIUM]

# Deterministic heuristics for RoutingRule conditions (governance/models.py)
# - no extra LLM call, unlike classify_complexity below, since these are
# meant to be instant/predictable admin-authored rules rather than another
# judgment call. Necessarily approximate: "looks like code" and "casual"
# are heuristics, not a real classifier.
_CODE_FENCE_RE = re.compile(r"```")
_CODE_KEYWORD_RE = re.compile(
    r"\b(def|function|class|import|const|let|var|return|elif|except|public|private|static)\b" r"|[{};]|=>|::",
)
_CASUAL_WORD_LIMIT = 8
# An attachment counts as a "long document" past this size - short/small
# attachments (a one-page snippet) don't trigger the "long document" rule.
_LONG_DOCUMENT_BYTES = 20_000
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


class NoModelAvailableError(Exception):
    pass


def _apply_zero_retention_filter(qs):
    """Data Handling's "only allow zero-retention models" toggle
    (governance/models.py::ComplianceSettings) - org-wide, not tied to any
    one user/department, so it's applied directly here rather than
    through governance/plans.py's per-user allow-list functions."""
    from governance.models import ComplianceSettings

    if ComplianceSettings.load().only_zero_retention_models:
        qs = qs.filter(provider__trains_on_data=False)
    return qs


def _allowed_models_for_user(user, tier=None):
    """Enabled models, cheapest-output-first, restricted to the user's Plan
    (plus/minus explicit UserModelPermission overrides, any Team.
    disabled_models restriction, and any Department Compliance Routing
    region restriction - see governance/plans.py for the exact
    precedence), and to zero-retention-only providers if that org-wide
    Data Handling toggle is on. Pass user=None to skip the permission
    filter entirely (used for the internal router call) - the
    zero-retention filter still applies either way, since it's not a
    per-user concern."""
    qs = ProviderModel.objects.filter(is_enabled=True)
    qs = _apply_zero_retention_filter(qs)
    if tier:
        qs = qs.filter(tier=tier)

    if user is not None:
        from governance.plans import effective_allowed_provider_model_ids, region_allowed_provider_model_ids

        allowed_ids = effective_allowed_provider_model_ids(user)
        if allowed_ids is not None:
            qs = qs.filter(id__in=allowed_ids)
        region_ids = region_allowed_provider_model_ids(user)
        if region_ids is not None:
            qs = qs.filter(id__in=region_ids)

    return qs.order_by(F("output_price_per_mtok").asc(nulls_last=True))


def classify_complexity(user_message: str) -> str:
    """Run the router classification prompt on the cheapest economy model.
    Never routes the classifier itself through a premium model."""
    router_model = _allowed_models_for_user(None, tier=ProviderModel.Tier.ECONOMY).first()
    if router_model is None:
        return ProviderModel.Tier.DEFAULT

    provider = get_provider(router_model.provider)
    prompt = ROUTER_CLASSIFICATION_PROMPT.format(user_message=user_message)
    try:
        raw = provider.complete([{"role": "user", "content": prompt}], router_model.model_id)
    except ProviderError:
        return ProviderModel.Tier.DEFAULT

    answer = raw.strip().lower()
    for tier in TIER_ORDER:
        if tier in answer:
            return tier
    return ProviderModel.Tier.DEFAULT


def select_model_candidates(user, tier: str) -> list[ProviderModel]:
    """Ordered list of allowed+enabled models to try: cheapest-first at
    `tier`, then cheapest-first at neighboring tiers as a fallback. Used
    both for the initial pick and for provider failover (spec: "if primary
    provider API fails, retry via secondary provider")."""
    seen_ids = set()
    candidates = []
    fallback_order = [tier] + [t for t in TIER_ORDER if t != tier]
    for candidate_tier in fallback_order:
        for model in _allowed_models_for_user(user, tier=candidate_tier):
            if model.id not in seen_ids:
                seen_ids.add(model.id)
                candidates.append(model)
    return candidates


def select_model_for_user(user, tier: str) -> ProviderModel:
    """Pick the single best candidate (see select_model_candidates)."""
    candidates = select_model_candidates(user, tier)
    if not candidates:
        raise NoModelAvailableError("No AI model is enabled and permitted for this user.")
    return candidates[0]


def _condition_matches(condition, message):
    """message is the Message row (role=user) this reply is answering, or
    None if the conversation has no user message yet."""
    content = message.content if message else ""
    # attachment_original_name (a plain CharField) rather than the
    # .attachment FileField itself - chat/views.py::post_message always
    # sets both together on a real upload, and checking the name avoids a
    # storage read just to answer "was anything attached".
    has_attachment = bool(message and message.attachment_original_name)
    is_image = has_attachment and any(
        message.attachment_original_name.lower().endswith(ext) for ext in _IMAGE_EXTENSIONS
    )

    if condition == "code_like":
        return bool(_CODE_FENCE_RE.search(content) or _CODE_KEYWORD_RE.search(content))
    if condition == "casual_short":
        return len(content.split()) <= _CASUAL_WORD_LIMIT and not _CODE_FENCE_RE.search(content)
    if condition == "image_attached":
        return is_image
    if condition == "long_document_attached":
        return has_attachment and not is_image and (message.attachment_size or 0) >= _LONG_DOCUMENT_BYTES
    return False


def match_routing_rule(user, conversation):
    """First active RoutingRule (governance/models.py), in priority order,
    whose condition matches the conversation's latest user message AND
    whose target_model this user can actually use. Returns None (falling
    through to tier-classification routing unchanged) when no rule
    matches, or when every matching rule's target_model turns out to be
    one this user isn't allowed to use - same fail-safe-to-normal-routing
    posture as the budget-automation override in chat/views.py."""
    from governance.models import RoutingRule

    rules = RoutingRule.objects.filter(is_active=True).select_related("target_model").order_by("priority", "id")
    if not rules:
        return None

    message = conversation.messages.filter(role="user").order_by("-created_at").first()
    visible_ids = set(models_visible_to_user(user).values_list("id", flat=True))

    for rule in rules:
        if _condition_matches(rule.condition, message) and rule.target_model_id in visible_ids:
            return rule.target_model
    return None


def models_visible_to_user(user):
    """Enabled models this user is allowed to pick from a manual model
    dropdown, cheapest-first within each tier."""
    from governance.plans import effective_allowed_provider_model_ids, region_allowed_provider_model_ids

    qs = ProviderModel.objects.filter(is_enabled=True)
    qs = _apply_zero_retention_filter(qs)
    allowed_ids = effective_allowed_provider_model_ids(user)
    if allowed_ids is not None:
        qs = qs.filter(id__in=allowed_ids)
    region_ids = region_allowed_provider_model_ids(user)
    if region_ids is not None:
        qs = qs.filter(id__in=region_ids)
    return qs.order_by("tier", F("output_price_per_mtok").asc(nulls_last=True))
