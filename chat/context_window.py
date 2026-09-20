"""Bounded conversation context for a chat request.

Before this module every reply loaded EVERY message of the conversation, re-read and re-parsed every
attachment (a PDF is re-parsed on every turn), and sent the lot; the only guard was a plan cap that
rejected the request, and plans without a cap had no bound at all.

What is sent now, in priority order (only what fits the budget):

  CURRENT MESSAGE      always kept, with its own attachment text. If it alone cannot fit, the
                       request is refused with a plain message - never silently cut.
  RECENT MESSAGES      the newest turns, verbatim, as many as fit.
  IMPORTANT CONTEXT    the conversation's opening message (the original brief), kept when older
                       turns are dropped.
  OLDER NOTES          a short extractive digest of what the user asked in the dropped turns. It is
                       NOT an AI summary (that would cost a provider call per long chat); it is the
                       first words of each earlier question, so the model knows the topics existed.

The notes travel inside the first retained user turn (same trust level as the user's own words),
never in the system prompt. The reply tells the user, once, that older messages are being condensed,
so nothing is dropped silently. Token counts are the same ~4 characters/token estimate the plan
limit already uses: an approximation, not a tokenizer.
"""

import hashlib
import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext_lazy

from chat.document_extraction import (
    EXTRACTABLE_EXTENSIONS,
    IMAGE_EXTENSIONS,
    extract_image,
    extract_text,
    wrap_for_prompt,
)

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 1000  # the same flat estimate governance.plans uses for the plan cap
OUTPUT_RESERVE_TOKENS = 8192  # room left for the reply (the providers ask for at most 8000)
MAX_RECENT_MESSAGES = 40  # never load more than this many messages verbatim
MAX_OLDER_MESSAGES_FOR_NOTES = 60  # how far back the digest looks beyond that
NOTE_CHARS_PER_MESSAGE = 140
NOTES_MAX_CHARS = 3000
BRIEF_MAX_CHARS = 1500
MAX_IMAGES = 4  # newest images only; older ones are mentioned, not re-sent
ATTACHMENT_TEXT_TTL = 6 * 3600
ATTACHMENT_FAILURE_TTL = 600

ANNOUNCE_TTL = 24 * 3600
CONTEXT_TRIMMED_NOTICE = gettext_lazy(
    "\n\n*(This conversation is long, so older messages are condensed to fit the model's memory; "
    "your recent messages are kept in full. Start a new chat for a clean slate.)*"
)
MESSAGE_TOO_LARGE = gettext_lazy(
    "This message is too large for the selected model's context window. Shorten it or send it in parts."
)


@dataclass
class Window:
    """Everything a request may draw on: the newest turns (already built, attachments included)
    and what is known about the turns before them."""

    turns: list
    older_count: int = 0  # messages that exist before `turns` (not loaded verbatim)
    brief: str = ""  # the conversation's opening user message, when it is older than `turns`
    older_notes: list = field(default_factory=list)  # first words of earlier user questions, oldest first


@dataclass
class Fitted:
    turns: list
    omitted: int  # how many earlier messages are no longer sent verbatim
    fits: bool  # False when the current message alone exceeds the budget
    tokens: int


def estimate_turn_tokens(turn):
    return len(turn.get("content", "")) // CHARS_PER_TOKEN + 4 + len(turn.get("images") or []) * IMAGE_TOKENS


def estimate_tokens(system_prompt, turns):
    return max(1, len(system_prompt) // CHARS_PER_TOKEN) + sum(estimate_turn_tokens(t) for t in turns)


# ---------------------------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------------------------
def model_context_tokens(provider_model):
    """The input budget for a model: a per-model override, else the adapter default, else the global
    default - never unlimited. The provider metadata that would say the real window is not stored,
    so these are configured assumptions (settings MODEL_CONTEXT_TOKENS*), deliberately conservative."""
    model_id = (provider_model.model_id or "").lower()
    for needle, tokens in getattr(settings, "MODEL_CONTEXT_TOKENS_BY_MODEL", {}).items():
        if needle.lower() in model_id:
            return int(tokens)
    adapter = provider_model.provider.adapter_type
    by_adapter = getattr(settings, "MODEL_CONTEXT_TOKENS", {})
    return int(by_adapter.get(adapter) or getattr(settings, "MODEL_CONTEXT_TOKENS_DEFAULT", 32000))


def model_budget(provider_model):
    """Tokens available for system prompt + history for this model (window minus the reply reserve)."""
    return max(1024, model_context_tokens(provider_model) - OUTPUT_RESERVE_TOKENS)


def plan_budget(user):
    from governance.plans import get_plan_status

    plan = get_plan_status(user)["plan"]
    return None if plan is None else plan.max_context_tokens


# ---------------------------------------------------------------------------------------------
# Loading (bounded) and attachments (cached)
# ---------------------------------------------------------------------------------------------
def _attachment_cache_key(msg):
    """Identifies THIS message's file at THIS state: message id (so a key can only ever be looked up
    by a request that already passed the conversation ownership check for that message), the
    stored name, and the file's size and modification time (so a replaced file misses the cache)."""
    storage = msg.attachment.storage
    name = msg.attachment.name
    stamp = f"{msg.pk}|{name}|{storage.size(name)}|{storage.get_modified_time(name).timestamp()}"
    return "chat:att:v1:" + hashlib.sha256(stamp.encode("utf-8")).hexdigest()


def attachment_text(msg, extension):
    """Extracted text for a message's attachment, computed once and reused; None if unreadable.
    Bounded (document_extraction.MAX_CHARS), never logged, and a failure is remembered briefly so
    a broken file is not re-parsed on every turn. A cache outage just means extracting again."""
    try:
        key = _attachment_cache_key(msg)
    except Exception:  # noqa: BLE001 - file missing or storage unreadable: nothing to cache or read
        return None
    try:
        hit = cache.get(key)
    except Exception:  # noqa: BLE001
        hit = None
    if hit is not None:
        return hit["text"]
    text = extract_text(msg.attachment, extension)
    try:
        cache.set(key, {"text": text}, ATTACHMENT_TEXT_TTL if text is not None else ATTACHMENT_FAILURE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return text


def _turn_for(msg, allow_image):
    content = msg.content
    images = None
    if msg.attachment:
        name = msg.attachment_original_name
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if extension in IMAGE_EXTENSIONS:
            if not allow_image:
                content = f"{content}\n\n[Earlier image: {name} (not re-sent, to save context)]"
            else:
                image = extract_image(msg.attachment, extension)
                if image is not None:
                    images = [image]
                else:
                    content = f"{content}\n\n[Attached image: {name} (couldn't be read)]"
        else:
            extracted = attachment_text(msg, extension) if extension in EXTRACTABLE_EXTENSIONS else None
            if extracted is not None:
                content = f"{content}\n\n{wrap_for_prompt(name, extracted)}"
            else:
                content = f"{content}\n\n[Attached file: {name} (not readable by the assistant yet)]"
    turn = {"role": msg.role, "content": content}
    if images:
        turn["images"] = images
    return turn


def _first_words(text, limit):
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def load_window(conversation, exclude_message_id):
    """The bounded window for this request: at most MAX_RECENT_MESSAGES turns are built (and only
    their attachments read), plus cheap text-only notes about anything older."""
    base = conversation.messages.exclude(id=exclude_message_id)
    total = base.count()
    recent = list(base.order_by("-created_at", "-id")[:MAX_RECENT_MESSAGES])  # newest first
    older_count = max(0, total - len(recent))

    turns, images_left = [], MAX_IMAGES
    for msg in recent:  # newest first, so the image allowance goes to the newest images
        wants_image = (
            bool(msg.attachment) and msg.attachment_original_name.rsplit(".", 1)[-1].lower() in IMAGE_EXTENSIONS
        )
        allow = images_left > 0
        turns.append(_turn_for(msg, allow_image=allow))
        if wants_image and allow and turns[-1].get("images"):
            images_left -= 1
    turns.reverse()

    window = Window(turns=turns, older_count=older_count)
    if older_count:
        first = base.filter(role="user").order_by("created_at", "id").values_list("content", flat=True).first()
        window.brief = _first_words(first, BRIEF_MAX_CHARS)
        # `recent` is the newest slice, so the user messages in it are the newest user messages: skip them.
        skip = sum(1 for m in recent if m.role == "user")
        older = (
            base.filter(role="user")
            .order_by("-created_at", "-id")
            .values_list("content", flat=True)[skip : skip + MAX_OLDER_MESSAGES_FOR_NOTES]
        )
        window.older_notes = [_first_words(c, NOTE_CHARS_PER_MESSAGE) for c in reversed(list(older)) if c.strip()]
    return window


# ---------------------------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------------------------
NOTES_RESERVE_TOKENS = 1200  # room set aside for the brief + notes whenever older turns are left out


def _notes_block(window, dropped_turns, omitted, max_chars):
    """The IMPORTANT CONTEXT + OLDER NOTES text placed ahead of the retained turns, at most max_chars."""
    header = f"[Earlier in this conversation, {omitted} message(s) were left out to fit the context window.]"
    lines = [header]
    remaining = max_chars - len(header)
    if window.brief and remaining > 80:
        brief = _first_words(window.brief, min(BRIEF_MAX_CHARS, remaining // 2))
        lines.append(f"Conversation began with: {brief}")
        remaining -= len(brief) + 28
    notes = list(window.older_notes) + [
        _first_words(t["content"], NOTE_CHARS_PER_MESSAGE) for t in dropped_turns if t["role"] == "user"
    ]
    kept, size = [], 0
    for note in reversed(notes):  # prefer the notes closest to the retained turns
        if size + len(note) + 3 > min(NOTES_MAX_CHARS, remaining):
            break
        kept.append(note)
        size += len(note) + 3
    if kept:
        lines.append("Earlier questions from the user, oldest first:")
        lines.extend(f"- {note}" for note in reversed(kept))
    return "\n".join(lines)


def fit(window, system_prompt, budget_tokens):
    """Choose what to send for `budget_tokens` (system prompt + history). Pure and cheap: it only
    measures and slices, so it can be repeated per candidate model with a different budget."""
    turns = window.turns
    if not turns:
        return Fitted([], 0, True, estimate_tokens(system_prompt, []))
    budget = float("inf") if budget_tokens is None else budget_tokens
    base = estimate_tokens(system_prompt, [turns[-1]])
    fits = base <= budget

    def fill(limit):
        """Newest turns that fit `limit`, oldest first; the current message is always kept."""
        kept_reversed, used = [turns[-1]], base
        if fits:
            for turn in reversed(turns[:-1]):
                cost = estimate_turn_tokens(turn)
                if used + cost > limit:
                    break
                kept_reversed.append(turn)
                used += cost
        kept = list(reversed(kept_reversed))
        # A provider conversation must open with the user's turn: drop a reply that lost its question.
        while len(kept) > 1 and kept[0]["role"] != "user":
            used -= estimate_turn_tokens(kept.pop(0))
        return kept, used

    kept, used = fill(budget)
    if fits and (window.older_count or len(kept) < len(turns)):
        # Something is being left out: set room aside for the brief and notes FIRST, so they are not
        # crowded out by the very turns they stand in for.
        reserve = 0 if budget == float("inf") else min(NOTES_RESERVE_TOKENS, max(0, int((budget - base) // 4)))
        kept, used = fill(budget - reserve)
        dropped = turns[: len(turns) - len(kept)]
        omitted = window.older_count + len(dropped)
        max_chars = (reserve if reserve else NOTES_RESERVE_TOKENS) * CHARS_PER_TOKEN
        note = _notes_block(window, dropped, omitted, max_chars)
        head = dict(kept[0])
        head["content"] = f"{note}\n\n{head['content']}"
        kept[0] = head
        used += len(note) // CHARS_PER_TOKEN
        return Fitted(kept, omitted, True, int(used))
    return Fitted(kept, window.older_count + len(turns) - len(kept), fits, int(used))


def should_announce(conversation_id):
    """True the first time in a day that a reply in this conversation had older messages condensed,
    so the user is told without the note being repeated under every reply. Language-independent
    (a cache flag, not a search of reply text); if the cache is down the note is shown."""
    try:
        return bool(cache.add(f"chat:ctxnote:{conversation_id}", 1, ANNOUNCE_TTL))
    except Exception:  # noqa: BLE001
        return True
