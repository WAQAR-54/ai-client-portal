"""Fetches the actual list of available models from each provider's own
API, so admins pick from real, exact IDs instead of hand-typing them - the
earlier incident (a typo'd model ID silently breaking chat) was exactly the
failure mode this replaces. Never creates or enables anything on its own;
callers decide what to do with the returned IDs."""

from django.conf import settings

from chat.models import ModelConfig

# Heuristic only - OpenAI's /v1/models response carries no capability flag
# to distinguish chat models from embeddings/audio/etc, so this excludes
# obviously non-chat families by name. Anthropic's list endpoint only ever
# returns chat-capable Claude models, so no filtering is needed there.
_OPENAI_NON_CHAT_MARKERS = [
    "embedding",
    "whisper",
    "tts",
    "dall-e",
    "moderation",
    "davinci-002",
    "babbage-002",
    "audio",
    "transcribe",
    "realtime",
    "search-preview",
]


def _is_openai_chat_model(model_id):
    lowered = model_id.lower()
    return not any(marker in lowered for marker in _OPENAI_NON_CHAT_MARKERS)


def fetch_openai_models():
    """Returns a sorted list of model ID strings, or None if no API key is
    configured. Raises on a real API/network failure - callers decide how
    to surface that."""
    if not settings.OPENAI_API_KEY:
        return None
    import openai

    client = openai.OpenAI(api_key=settings.OPENAI_API_KEY, timeout=20.0)
    response = client.models.list()
    return sorted({m.id for m in response.data if _is_openai_chat_model(m.id)})


def fetch_anthropic_models():
    if not settings.ANTHROPIC_API_KEY:
        return None
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=20.0)
    # Default page size is small (20) - this endpoint is paginated, so pass
    # the max limit to avoid silently missing models further down the list.
    response = client.models.list(limit=1000)
    return sorted({m.id for m in response.data})


_FETCHERS = {
    ModelConfig.Provider.OPENAI: fetch_openai_models,
    ModelConfig.Provider.ANTHROPIC: fetch_anthropic_models,
}


def fetch_all_available_models():
    """{"openai": {"configured": bool, "models": [...], "error": str|None}, "anthropic": {...}}"""
    result = {}
    for provider, fetch_fn in _FETCHERS.items():
        entry = {"configured": True, "models": [], "error": None}
        try:
            models = fetch_fn()
            if models is None:
                entry["configured"] = False
            else:
                entry["models"] = models
        except Exception as exc:
            entry["error"] = str(exc)
        result[provider] = entry
    return result


def known_model_keys():
    """Set of (provider, model_name) tuples already tracked as ModelConfig rows."""
    return set(ModelConfig.objects.values_list("provider", "model_name"))
