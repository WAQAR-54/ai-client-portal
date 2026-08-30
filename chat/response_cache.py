"""Exact-match caching for AI responses, to avoid re-billing an identical
call (see config/settings.py CACHES for the Redis wiring).

Cache key is a hash of (user, model, system prompt, entire message history)
- not just the trailing user message. Two different conversations can share
the exact same latest message ("yes", "continue", "explain more") with
completely different meaning; keying on that alone would silently serve one
user's cached answer into an unrelated context. Hashing the full history
means only a genuinely identical exchange - same user, same model, same
conversation so far - ever hits.

Never raises: a Redis hiccup should degrade to "no caching", not break chat.
"""

import hashlib
import json
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60 * 60  # 1 hour
_KEY_PREFIX = "resp_cache"


def _cache_key(user_id, model_config_id, system_prompt, history):
    payload = json.dumps(
        {"model": model_config_id, "system": system_prompt, "history": history},
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{user_id}:{digest}"


def get_cached_response(user_id, model_config_id, system_prompt, history):
    """Returns {"text", "input_tokens", "output_tokens"} on a hit, else None."""
    try:
        return cache.get(_cache_key(user_id, model_config_id, system_prompt, history))
    except Exception:
        logger.exception("Response cache read failed; treating as a miss.")
        return None


def store_cached_response(user_id, model_config_id, system_prompt, history, *, text, input_tokens, output_tokens):
    try:
        cache.set(
            _cache_key(user_id, model_config_id, system_prompt, history),
            {"text": text, "input_tokens": input_tokens, "output_tokens": output_tokens},
            timeout=CACHE_TTL_SECONDS,
        )
    except Exception:
        logger.exception("Response cache write failed; continuing without caching.")
