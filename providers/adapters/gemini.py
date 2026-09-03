import requests

from providers.adapters.base import BaseProviderAdapter, ProviderAPIError
from providers.adapters.sanitize import sanitize_error

# Google's Generative Language API (the plain-API-key surface - distinct
# from Vertex AI, which uses GCP service-account auth and is out of scope
# here). No official Python SDK is a pre-existing dependency in this
# project, so this calls the documented REST endpoint directly via
# `requests` (already a dependency) rather than adding a new SDK for one
# list-models call.
_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiAdapter(BaseProviderAdapter):
    def test_connection(self, api_key):
        try:
            resp = requests.get(f"{_BASE_URL}/models", params={"key": api_key, "pageSize": 1}, timeout=20)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def fetch_models(self, api_key):
        try:
            resp = requests.get(f"{_BASE_URL}/models", params={"key": api_key, "pageSize": 1000}, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderAPIError(sanitize_error(str(exc), api_key)) from exc

        data = resp.json()
        results = []
        for m in data.get("models", []):
            # "models/gemini-1.5-pro" -> "gemini-1.5-pro" - the API-key
            # surface prefixes every model name with "models/"; adapters
            # for the other two providers return bare IDs, so this strips
            # it for a consistent model_id shape across all adapters.
            raw_name = m.get("name", "")
            model_id = raw_name.split("/", 1)[1] if "/" in raw_name else raw_name
            if not model_id:
                continue
            # Only chat-capable models - the response includes embedding
            # and other non-generative models too, distinguished by this
            # capability flag (unlike OpenAI's /v1/models, which has none).
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            results.append(
                {
                    "model_id": model_id,
                    "display_name": m.get("displayName") or model_id,
                    # No pricing in this response - same "admin fills it
                    # in after import" pattern as every other adapter here.
                    "input_price": None,
                    "output_price": None,
                }
            )
        return results
