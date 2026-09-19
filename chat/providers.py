"""Common streaming interface over every connected AI provider's chat API.

Callers depend only on `get_provider(provider_row).stream_chat(...)` and
never touch a provider SDK/API directly, so the rest of the app stays
provider-agnostic. `provider_row` is a providers.models.Provider instance
(what ProviderModel.provider resolves to) - each AIProvider subclass reads
its API key exclusively from that row (provider_row.get_decrypted_key()).
There is no settings.py/env-var fallback for any provider (including
OpenAI/Anthropic, which briefly had one during the ModelConfig -> Provider
migration) - every provider must be connected through the Providers admin
page before it can be used, full stop.
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Gemini is called directly via `requests` (no vendor SDK, see
# GeminiProvider's own docstring) - OpenAI/Anthropic's SDKs already retry
# transient failures on their own (max_retries=5, set where each client is
# built below), so this gives the raw-requests path the same resilience
# instead of failing immediately on the first dropped connection/5xx/429.
# Retries the CONNECTION/initial response only, same limitation every one
# of these retry strategies has for a streamed response - a connection
# that drops mid-stream, after headers already came back, isn't retried.
_RETRYING_SESSION = requests.Session()
_RETRYING_SESSION.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
        )
    ),
)


@dataclass
class StreamChunk:
    text: str = ""
    done: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProviderError(Exception):
    """Raised when an upstream AI provider call fails."""


class AIProvider(ABC):
    def __init__(self, provider_row=None):
        self.provider_row = provider_row

    @abstractmethod
    def stream_chat(
        self, messages: list[dict], model_name: str, system_prompt: str = "", enable_web_search: bool = False
    ) -> Iterator[StreamChunk]:
        """Yield StreamChunk(text=...) for each token/segment, then one final
        StreamChunk(done=True, input_tokens=..., output_tokens=...).

        enable_web_search requests Research mode (chat/views.py::
        stream_message) - live web search grounded in current sources.
        Only AnthropicProvider below actually acts on it (Claude's native
        web_search server tool); every other provider accepts and quietly
        ignores the flag rather than erroring, since stream_message already
        restricts Research mode's candidates to Anthropic models before
        this is ever called with enable_web_search=True on anything else."""

    @abstractmethod
    def complete(
        self, messages: list[dict], model_name: str, system_prompt: str = "", enable_web_search: bool = False
    ) -> str:
        """Non-streaming single-shot completion, used by the smart router classifier."""


class OpenAICompatibleProvider(AIProvider):
    """OpenAI itself, xAI Grok, DeepSeek, or any future custom OpenAI-
    compatible provider - identical wire format, just a different
    base_url per Provider row (OpenAI's own seeded row already carries
    "https://api.openai.com/v1")."""

    def _api_key(self):
        key = self.provider_row.get_decrypted_key() if self.provider_row is not None else None
        if not key:
            raise ProviderError(f"{self.provider_row.name if self.provider_row else 'Provider'} is not connected.")
        return key

    def _client(self):
        import openai

        # The SDK's default max_retries=2 gave up too fast against a flaky
        # local network (observed: two quick retries on a DNS getaddrinfo
        # failure, then a hard failure) — a real, live example of this is
        # in the incident notes. Bumped so a transient blip doesn't
        # immediately surface as a failed reply to the user.
        return openai.OpenAI(
            api_key=self._api_key(),
            base_url=self.provider_row.base_url or None,
            max_retries=5,
            timeout=60.0,
        )

    def _format_messages(self, messages, system_prompt):
        formatted = []
        if system_prompt:
            formatted.append({"role": "system", "content": system_prompt})
        for m in messages:
            images = m.get("images")
            if not images:
                formatted.append({"role": m["role"], "content": m["content"]})
                continue
            # OpenAI's vision format: content becomes a list of typed
            # parts instead of a plain string, one part per image plus
            # one for the text (data URIs, so no publicly-reachable URL
            # is ever needed for our own-hosted attachments).
            parts = [{"type": "text", "text": m["content"]}] if m["content"] else []
            for image in images:
                parts.append(
                    {"type": "image_url", "image_url": {"url": f"data:{image['mime_type']};base64,{image['data']}"}}
                )
            formatted.append({"role": m["role"], "content": parts})
        return formatted

    def stream_chat(self, messages, model_name, system_prompt="", enable_web_search=False):
        # enable_web_search is a no-op here - see AIProvider.stream_chat's
        # own docstring for why. Real web search over the Chat Completions
        # API this class uses would need OpenAI's separate Responses API
        # (a different call shape entirely), not attempted in this pass.
        try:
            stream = self._client().chat.completions.create(
                model=model_name,
                messages=self._format_messages(messages, system_prompt),
                stream=True,
                stream_options={"include_usage": True},
            )
            input_tokens = output_tokens = None
            for chunk in stream:
                if chunk.usage is not None:
                    input_tokens = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens
                if chunk.choices and chunk.choices[0].delta.content:
                    yield StreamChunk(text=chunk.choices[0].delta.content)
            yield StreamChunk(done=True, input_tokens=input_tokens, output_tokens=output_tokens)
        except Exception as exc:
            raise ProviderError(str(exc)) from exc

    def complete(self, messages, model_name, system_prompt="", enable_web_search=False):
        try:
            response = self._client().chat.completions.create(
                model=model_name,
                messages=self._format_messages(messages, system_prompt),
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            raise ProviderError(str(exc)) from exc


class AnthropicProvider(AIProvider):
    def _api_key(self):
        key = self.provider_row.get_decrypted_key() if self.provider_row is not None else None
        if not key:
            raise ProviderError(f"{self.provider_row.name if self.provider_row else 'Provider'} is not connected.")
        return key

    def _client(self):
        import anthropic

        return anthropic.Anthropic(api_key=self._api_key(), max_retries=5, timeout=60.0)

    def _format_messages(self, messages):
        formatted = []
        for m in messages:
            images = m.get("images")
            if not images:
                formatted.append({"role": m["role"], "content": m["content"]})
                continue
            # Anthropic's vision format: content becomes a list of typed
            # blocks - image blocks first, then text, matching Anthropic's
            # own documented recommendation for image-then-text ordering.
            parts = [
                {"type": "image", "source": {"type": "base64", "media_type": image["mime_type"], "data": image["data"]}}
                for image in images
            ]
            if m["content"]:
                parts.append({"type": "text", "text": m["content"]})
            formatted.append({"role": m["role"], "content": parts})
        return formatted

    # Basic (non-dynamic-filtering) version of Anthropic's native web_search
    # server tool - see chat/views.py::stream_message for how Research mode
    # reaches here. text_stream already yields only "text"-type content
    # block deltas, so adding this tool needs no other change to the
    # streaming loop below: server_tool_use/web_search_tool_result blocks
    # (the search query/results themselves) simply aren't text blocks and
    # never appear in text_stream - they're only visible via
    # get_final_message().content afterward, which this doesn't currently
    # surface further (no citations UI in this pass - just the grounded
    # answer text). max_uses caps searches per turn as a cost guard.
    _WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

    def stream_chat(self, messages, model_name, system_prompt="", enable_web_search=False):
        try:
            kwargs = {"system": system_prompt} if system_prompt else {}
            if enable_web_search:
                kwargs["tools"] = [self._WEB_SEARCH_TOOL]
            with self._client().messages.stream(
                model=model_name,
                # A search-grounded answer citing several sources tends to
                # run longer than a plain reply - the same reasoning as
                # complete()'s own bump below, just for the streaming path.
                max_tokens=8000 if enable_web_search else 4096,
                messages=self._format_messages(messages),
                **kwargs,
            ) as stream:
                for text in stream.text_stream:
                    yield StreamChunk(text=text)
                final = stream.get_final_message()
                yield StreamChunk(
                    done=True,
                    input_tokens=final.usage.input_tokens,
                    output_tokens=final.usage.output_tokens,
                )
        except Exception as exc:
            raise ProviderError(str(exc)) from exc

    def complete(self, messages, model_name, system_prompt="", enable_web_search=False):
        try:
            kwargs = {"system": system_prompt} if system_prompt else {}
            if enable_web_search:
                kwargs["tools"] = [self._WEB_SEARCH_TOOL]
            response = self._client().messages.create(
                model=model_name,
                max_tokens=2048 if enable_web_search else 1024,
                messages=self._format_messages(messages),
                **kwargs,
            )
            return "".join(block.text for block in response.content if block.type == "text")
        except Exception as exc:
            raise ProviderError(str(exc)) from exc


class GeminiProvider(AIProvider):
    """Google's Generative Language REST API (the plain-API-key surface,
    not Vertex AI). Built directly against Google's published
    streamGenerateContent/generateContent docs via `requests` (no Google
    SDK is an existing dependency - see providers/adapters/gemini.py for
    the same reasoning on the model-listing side).

    NOT verified against a real Gemini API key in this environment - the
    request/response shapes below are Google's documented format, but
    this has not been exercised against a live call. Treat as unverified
    until tested with a real key, same caveat as providers/adapters/
    gemini.py's model-listing adapter.
    """

    _BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def _api_key(self):
        key = self.provider_row.get_decrypted_key() if self.provider_row is not None else None
        if not key:
            raise ProviderError(f"{self.provider_row.name if self.provider_row else 'Provider'} is not connected.")
        return key

    def _to_gemini_contents(self, messages):
        # {"role": "user"|"assistant", "content": str} -> Gemini's
        # {"role": "user"|"model", "parts": [{"text": str}]} - "assistant"
        # is "model" in Gemini's vocabulary, everything else (only "user"
        # is ever sent by this app) passes through unchanged. An "images"
        # key adds one inline_data part per image alongside the text part.
        contents = []
        for m in messages:
            role = "model" if m["role"] == "assistant" else "user"
            parts = [{"text": m["content"]}] if m["content"] else []
            for image in m.get("images") or []:
                parts.append({"inline_data": {"mime_type": image["mime_type"], "data": image["data"]}})
            contents.append({"role": role, "parts": parts})
        return contents

    def _body(self, messages, system_prompt):
        body = {"contents": self._to_gemini_contents(messages)}
        if system_prompt:
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        return body

    def stream_chat(self, messages, model_name, system_prompt="", enable_web_search=False):
        # enable_web_search is a no-op here - see AIProvider.stream_chat's
        # own docstring. Gemini has its own native Google Search grounding
        # tool, but wiring it up is out of scope for this pass.
        url = f"{self._BASE_URL}/models/{model_name}:streamGenerateContent"
        try:
            resp = _RETRYING_SESSION.post(
                url,
                params={"key": self._api_key(), "alt": "sse"},
                json=self._body(messages, system_prompt),
                stream=True,
                timeout=60,
            )
            resp.raise_for_status()
            input_tokens = output_tokens = None
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                chunk = json.loads(line[len("data: ") :])
                candidates = chunk.get("candidates") or []
                if candidates:
                    for part in candidates[0].get("content", {}).get("parts", []):
                        text = part.get("text", "")
                        if text:
                            yield StreamChunk(text=text)
                usage = chunk.get("usageMetadata")
                if usage:
                    input_tokens = usage.get("promptTokenCount")
                    output_tokens = usage.get("candidatesTokenCount")
            yield StreamChunk(done=True, input_tokens=input_tokens, output_tokens=output_tokens)
        except requests.RequestException as exc:
            raise ProviderError(str(exc)) from exc

    def complete(self, messages, model_name, system_prompt="", enable_web_search=False):
        url = f"{self._BASE_URL}/models/{model_name}:generateContent"
        try:
            resp = _RETRYING_SESSION.post(
                url,
                params={"key": self._api_key()},
                json=self._body(messages, system_prompt),
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
        except requests.RequestException as exc:
            raise ProviderError(str(exc)) from exc


# Keyed by Provider.adapter_type - deliberately the SAME dispatch key the
# model-listing adapters use (providers/adapters/__init__.py), since
# "which wire format does this provider speak" is the same question on
# both sides. openai_compatible covers OpenAI itself, Grok, and DeepSeek -
# one class, since none of the three has any special-cased key resolution
# left to distinguish them by.
_ADAPTER_TYPE_PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "gemini": GeminiProvider,
}


def get_provider(provider_row) -> AIProvider:
    """`provider_row` is a providers.models.Provider instance (what
    ProviderModel.provider resolves to for any model returned by
    chat/router.py or governance-selected in chat/views.py)."""
    adapter_type = getattr(provider_row, "adapter_type", None)
    try:
        provider_class = _ADAPTER_TYPE_PROVIDERS[adapter_type]
    except KeyError:
        raise ProviderError(f"Unknown provider adapter_type: {adapter_type!r}")
    return provider_class(provider_row)
