from providers.adapters.anthropic import AnthropicAdapter
from providers.adapters.base import BaseProviderAdapter, ProviderAPIError
from providers.adapters.gemini import GeminiAdapter
from providers.adapters.openai_compatible import OpenAICompatibleAdapter

# Keyed by Provider.adapter_type. Adding a new *kind* of API (not just a
# new OpenAI-compatible provider - see openai_compatible.py's own comment
# for that, much more common case) means a new adapter class plus one
# entry here.
_ADAPTERS = {
    "anthropic": AnthropicAdapter,
    "openai_compatible": OpenAICompatibleAdapter,
    "gemini": GeminiAdapter,
}


def get_adapter_class(adapter_type):
    try:
        return _ADAPTERS[adapter_type]
    except KeyError:
        raise ValueError(f"No adapter registered for adapter_type={adapter_type!r}")


__all__ = ["BaseProviderAdapter", "ProviderAPIError", "get_adapter_class"]
