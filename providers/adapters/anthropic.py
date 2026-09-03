from providers.adapters.base import BaseProviderAdapter, ProviderAPIError
from providers.adapters.sanitize import sanitize_error


class AnthropicAdapter(BaseProviderAdapter):
    """Same client/call already validated in chat/model_sync.py:
    fetch_anthropic_models - reused here rather than hand-rolling a raw
    HTTP call against Anthropic's models endpoint."""

    def _client(self, api_key):
        import anthropic

        return anthropic.Anthropic(api_key=api_key, timeout=20.0)

    def test_connection(self, api_key):
        try:
            self._client(api_key).models.list(limit=1)
            return True
        except Exception:
            return False

    def fetch_models(self, api_key):
        import anthropic

        try:
            # Default page size is small (20) - this endpoint is paginated,
            # so pass the max limit to avoid silently missing models
            # further down the list (same reasoning as the existing
            # chat/model_sync.py fetch).
            response = self._client(api_key).models.list(limit=1000)
        except anthropic.APIError as exc:
            raise ProviderAPIError(sanitize_error(str(exc), api_key)) from exc
        return [
            {
                "model_id": m.id,
                "display_name": getattr(m, "display_name", "") or m.id,
                # Anthropic's models.list response carries no pricing data -
                # same as the existing chat/model_sync.py's OpenAI/Anthropic
                # fetch, and matching the deliberate "never auto-fill
                # pricing" decision already in this codebase (see
                # model_sync.html) - an admin sets it after import.
                "input_price": None,
                "output_price": None,
            }
            for m in response.data
        ]
