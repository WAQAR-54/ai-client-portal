from providers.adapters.base import BaseProviderAdapter, ProviderAPIError
from providers.adapters.sanitize import sanitize_error

# Every provider here speaks the same /v1/models endpoint as OpenAI itself
# (documented explicitly by each), so adding e.g. Mistral, Groq, or a
# self-hosted vLLM/Ollama endpoint later needs zero new adapter code -
# just a new Provider row with adapter_type=openai_compatible and the
# right base_url. These two are the built-in defaults so a Provider row
# doesn't need base_url filled in for them specifically.
_BUILTIN_BASE_URLS = {
    "grok": "https://api.x.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
}

# Same heuristic already used in chat/model_sync.py for OpenAI itself - the
# /v1/models response carries no capability flag distinguishing chat
# models from embeddings/audio/image/moderation, so this excludes obvious
# non-chat families by name.
_NON_CHAT_MARKERS = [
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


def _is_chat_model(model_id):
    lowered = model_id.lower()
    return not any(marker in lowered for marker in _NON_CHAT_MARKERS)


class OpenAICompatibleAdapter(BaseProviderAdapter):
    """Same client/call already validated in chat/model_sync.py:
    fetch_openai_models, generalized with a configurable base_url - the
    openai Python SDK's base_url override is the documented, standard way
    every OpenAI-compatible provider (xAI, DeepSeek, and OpenAI itself)
    expects to be called."""

    def _base_url(self):
        return self.provider.base_url or _BUILTIN_BASE_URLS.get(self.provider.slug)

    def _client(self, api_key):
        import openai

        return openai.OpenAI(api_key=api_key, base_url=self._base_url(), timeout=20.0)

    def test_connection(self, api_key):
        try:
            self._client(api_key).models.list()
            return True
        except Exception:
            return False

    def fetch_models(self, api_key):
        import openai

        try:
            response = self._client(api_key).models.list()
        except openai.APIError as exc:
            raise ProviderAPIError(sanitize_error(str(exc), api_key)) from exc
        return [
            {
                "model_id": m.id,
                "display_name": m.id,
                # /v1/models carries no pricing data on any of these
                # providers - same deliberate "admin fills it in after
                # import" pattern as the Anthropic adapter and the
                # existing chat/model_sync.py.
                "input_price": None,
                "output_price": None,
            }
            for m in response.data
            if _is_chat_model(m.id)
        ]
