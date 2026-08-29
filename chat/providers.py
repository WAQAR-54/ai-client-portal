"""Common streaming interface over the OpenAI and Anthropic chat APIs.

Callers depend only on `get_provider(name).stream_chat(...)` and never touch
the OpenAI/Anthropic SDKs directly, so the rest of the app is provider-agnostic.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

from django.conf import settings


@dataclass
class StreamChunk:
    text: str = ""
    done: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProviderError(Exception):
    """Raised when an upstream AI provider call fails."""


class AIProvider(ABC):
    @abstractmethod
    def stream_chat(self, messages: list[dict], model_name: str, system_prompt: str = "") -> Iterator[StreamChunk]:
        """Yield StreamChunk(text=...) for each token/segment, then one final
        StreamChunk(done=True, input_tokens=..., output_tokens=...)."""

    @abstractmethod
    def complete(self, messages: list[dict], model_name: str, system_prompt: str = "") -> str:
        """Non-streaming single-shot completion, used by the smart router classifier."""


class OpenAIProvider(AIProvider):
    def _client(self):
        import openai

        # The SDK's default max_retries=2 gave up too fast against a flaky
        # local network (observed: two quick retries on a DNS getaddrinfo
        # failure, then a hard failure) — a real, live example of this is
        # in the incident notes. Bumped so a transient blip doesn't
        # immediately surface as a failed reply to the user.
        return openai.OpenAI(api_key=settings.OPENAI_API_KEY, max_retries=5, timeout=60.0)

    def _format_messages(self, messages, system_prompt):
        formatted = []
        if system_prompt:
            formatted.append({"role": "system", "content": system_prompt})
        formatted.extend(messages)
        return formatted

    def stream_chat(self, messages, model_name, system_prompt=""):
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

    def complete(self, messages, model_name, system_prompt=""):
        try:
            response = self._client().chat.completions.create(
                model=model_name,
                messages=self._format_messages(messages, system_prompt),
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            raise ProviderError(str(exc)) from exc


class AnthropicProvider(AIProvider):
    def _client(self):
        import anthropic

        return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=5, timeout=60.0)

    def stream_chat(self, messages, model_name, system_prompt=""):
        try:
            kwargs = {"system": system_prompt} if system_prompt else {}
            with self._client().messages.stream(
                model=model_name,
                max_tokens=4096,
                messages=messages,
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

    def complete(self, messages, model_name, system_prompt=""):
        try:
            kwargs = {"system": system_prompt} if system_prompt else {}
            response = self._client().messages.create(
                model=model_name,
                max_tokens=1024,
                messages=messages,
                **kwargs,
            )
            return "".join(block.text for block in response.content if block.type == "text")
        except Exception as exc:
            raise ProviderError(str(exc)) from exc


_PROVIDERS = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
}


def get_provider(name: str) -> AIProvider:
    try:
        return _PROVIDERS[name]()
    except KeyError:
        raise ProviderError(f"Unknown provider: {name}")
