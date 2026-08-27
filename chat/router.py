"""Smart routing: classify request complexity, then pick the cheapest
enabled model at that tier the requesting user is permitted to use.
"""
from django.db.models import F

from chat.models import ModelConfig, UserModelPermission
from chat.prompts import ROUTER_CLASSIFICATION_PROMPT
from chat.providers import ProviderError, get_provider

TIER_ORDER = [ModelConfig.Tier.ECONOMY, ModelConfig.Tier.DEFAULT, ModelConfig.Tier.PREMIUM]


class NoModelAvailableError(Exception):
    pass


def _allowed_models_for_user(user, tier=None):
    """Enabled models, cheapest-output-first, minus any this user is explicitly denied.
    Pass user=None to skip the permission filter (used for the internal router call)."""
    qs = ModelConfig.objects.filter(is_enabled=True)
    if tier:
        qs = qs.filter(tier=tier)

    if user is not None:
        denied_ids = UserModelPermission.objects.filter(
            user=user, is_allowed=False,
        ).values_list("model_config_id", flat=True)
        qs = qs.exclude(id__in=denied_ids)

    return qs.order_by(F("output_cost_per_1m").asc(nulls_last=True))


def classify_complexity(user_message: str) -> str:
    """Run the router classification prompt on the cheapest economy model.
    Never routes the classifier itself through a premium model."""
    router_model = _allowed_models_for_user(None, tier=ModelConfig.Tier.ECONOMY).first()
    if router_model is None:
        return ModelConfig.Tier.DEFAULT

    provider = get_provider(router_model.provider)
    prompt = ROUTER_CLASSIFICATION_PROMPT.format(user_message=user_message)
    try:
        raw = provider.complete([{"role": "user", "content": prompt}], router_model.model_name)
    except ProviderError:
        return ModelConfig.Tier.DEFAULT

    answer = raw.strip().lower()
    for tier in TIER_ORDER:
        if tier in answer:
            return tier
    return ModelConfig.Tier.DEFAULT


def select_model_candidates(user, tier: str) -> list[ModelConfig]:
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


def select_model_for_user(user, tier: str) -> ModelConfig:
    """Pick the single best candidate (see select_model_candidates)."""
    candidates = select_model_candidates(user, tier)
    if not candidates:
        raise NoModelAvailableError("No AI model is enabled and permitted for this user.")
    return candidates[0]
