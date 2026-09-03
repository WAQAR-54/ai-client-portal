class ProviderAPIError(Exception):
    """Raised by an adapter on a real API/network failure. Always sanitized
    (see providers/services.py) before being stored anywhere - never
    construct one by interpolating the raw API key into the message."""


class BaseProviderAdapter:
    """One instance per Provider row, wrapping that row's own base_url/
    credentials. Subclasses implement test_connection/fetch_models against
    a specific provider API; providers/services.py:sync_provider is the
    only caller that matters for fetch_models - test_connection is used by
    the Connect flow before a key is ever saved."""

    def __init__(self, provider):
        self.provider = provider

    def test_connection(self, api_key: str) -> bool:
        """True if `api_key` can actually authenticate against this
        provider - called with a freshly-pasted key BEFORE it's encrypted
        and saved, so a bad key never gets stored as "connected"."""
        raise NotImplementedError

    def fetch_models(self, api_key: str) -> list[dict]:
        """[{"model_id": str, "display_name": str, "input_price": Decimal|None,
        "output_price": Decimal|None}, ...] - never raises for "provider
        has zero models" (returns []), only for a genuine API/network
        failure (raises ProviderAPIError, message already sanitized of the
        raw key)."""
        raise NotImplementedError
