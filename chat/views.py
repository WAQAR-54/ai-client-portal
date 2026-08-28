from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import escape
from django.views.decorators.http import require_GET, require_http_methods
from sentry_sdk import capture_exception

from chat.models import Conversation, Message, ModelConfig
from chat.prompts import build_system_prompt
from chat.providers import ProviderError, get_provider
from chat.router import NoModelAvailableError, classify_complexity, models_visible_to_user, select_model_candidates
from governance.limits import UploadRejected, UsageLimitExceeded, check_usage_limits, validate_upload


def _owned_conversation_or_404(request, conversation_id):
    return get_object_or_404(Conversation, id=conversation_id, user=request.user)


@login_required
@require_GET
def download_attachment(request, conversation_id, message_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(Message, id=message_id, conversation=conversation)
    if not message.attachment:
        raise Http404
    return FileResponse(
        message.attachment.open("rb"), as_attachment=True,
        filename=message.attachment_original_name or message.attachment.name,
    )


@login_required
def chat_home(request, conversation_id=None):
    conversation = None
    if conversation_id:
        conversation = _owned_conversation_or_404(request, conversation_id)

    available_models = models_visible_to_user(request.user)
    context = {
        "conversations": Conversation.objects.filter(user=request.user),
        "conversation": conversation,
        "messages_list": conversation.messages.all() if conversation else [],
        "has_models_available": available_models.exists(),
        "available_models": available_models,
    }
    return render(request, "chat/chat_home.html", context)


@login_required
@require_http_methods(["POST"])
def create_conversation(request):
    conversation = Conversation.objects.create(user=request.user, title="New chat")
    return redirect("chat:chat_conversation", conversation_id=conversation.id)


@login_required
@require_http_methods(["POST"])
def post_message(request, conversation_id):
    content = request.POST.get("content", "").strip()
    uploaded_file = request.FILES.get("attachment")
    if not content and not uploaded_file:
        return render(
            request, "chat/_limit_exceeded.html",
            {"message": "Type a message or attach a file first."}, status=400,
        )

    if uploaded_file:
        try:
            validate_upload(request.user, uploaded_file)
        except UploadRejected as exc:
            return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=400)

    # Only offer a manually-picked model if it's actually one this user is
    # currently allowed to use — otherwise silently fall back to auto-routing
    # rather than trusting a stale/tampered value from the form.
    model_id = request.POST.get("model_id", "").strip()
    if model_id and not models_visible_to_user(request.user).filter(id=model_id).exists():
        model_id = ""

    # Lock the conversation row for the duration of the check+create so two
    # concurrent sends against the same conversation can't both pass the
    # session_limit check before either message is committed. (Postgres
    # only — SQLite ignores select_for_update, so this is a no-op in local
    # dev but takes effect once the app runs against the production DB.)
    with transaction.atomic():
        conversation = get_object_or_404(
            Conversation.objects.select_for_update(), id=conversation_id, user=request.user,
        )
        try:
            check_usage_limits(request.user, conversation)
        except UsageLimitExceeded as exc:
            return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=429)

        user_message = Message.objects.create(conversation=conversation, role=Message.Role.USER, content=content)
        if uploaded_file:
            user_message.attachment = uploaded_file
            user_message.attachment_original_name = uploaded_file.name
            user_message.attachment_size = uploaded_file.size
            user_message.save(update_fields=["attachment", "attachment_original_name", "attachment_size"])

        if conversation.title == "New chat":
            conversation.title = (content or uploaded_file.name)[:60]
            conversation.save(update_fields=["title"])

        pending_assistant_message = Message.objects.create(
            conversation=conversation, role=Message.Role.ASSISTANT, content="",
        )

    return render(request, "chat/_message_pending.html", {
        "conversation": conversation,
        "pending_message": pending_assistant_message,
        "user_message": user_message,
        "model_id": model_id,
    })


TEXT_ATTACHMENT_EXTENSIONS = {"txt", "csv", "md", "json"}
MAX_ATTACHMENT_CHARS_IN_PROMPT = 8000


def _history_with_attachments(conversation, exclude_message_id):
    """Message history as provider-ready dicts, with the text content of
    any small text-like attachment (txt/csv/md/json) appended inline.
    Other file types (images, PDFs, office docs) are stored and shown in
    the UI but not read by the model yet."""
    history = []
    messages = conversation.messages.exclude(id=exclude_message_id).order_by("created_at")
    for msg in messages:
        content = msg.content
        if msg.attachment:
            extension = msg.attachment_original_name.rsplit(".", 1)[-1].lower() if "." in msg.attachment_original_name else ""
            if extension in TEXT_ATTACHMENT_EXTENSIONS:
                try:
                    with msg.attachment.open("rb") as f:
                        file_text = f.read(MAX_ATTACHMENT_CHARS_IN_PROMPT + 1).decode("utf-8", errors="replace")
                    if len(file_text) > MAX_ATTACHMENT_CHARS_IN_PROMPT:
                        file_text = file_text[:MAX_ATTACHMENT_CHARS_IN_PROMPT] + "\n[...truncated...]"
                    content = f"{content}\n\n[Attached file: {msg.attachment_original_name}]\n{file_text}"
                except (FileNotFoundError, OSError):
                    content = f"{content}\n\n[Attached file: {msg.attachment_original_name} — could not be read]"
            else:
                content = f"{content}\n\n[Attached file: {msg.attachment_original_name} (not readable by the assistant yet)]"
        history.append({"role": msg.role, "content": content})
    return history


@login_required
@require_GET
def stream_message(request, conversation_id, message_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(
        Message, id=message_id, conversation=conversation, role=Message.Role.ASSISTANT, content="",
    )

    history = _history_with_attachments(conversation, exclude_message_id=message.id)
    requested_model_id = request.GET.get("model_id", "").strip()

    def event_stream():
        try:
            if requested_model_id and models_visible_to_user(request.user).filter(id=requested_model_id).exists():
                candidates = [ModelConfig.objects.get(id=requested_model_id)]
            else:
                tier = classify_complexity(history[-1]["content"] if history else "")
                candidates = select_model_candidates(request.user, tier)
        except NoModelAvailableError as exc:
            yield _sse_event("error", str(exc))
            return
        if not candidates:
            yield _sse_event("error", "No AI model is enabled and permitted for this user.")
            return

        system_prompt = build_system_prompt(request.user)

        # Provider fallback: if a candidate fails before it has streamed any
        # visible text, silently retry the next cheapest candidate (which is
        # usually the other provider) rather than surfacing a raw error —
        # per spec, "if primary provider API fails, retry via secondary
        # provider". Once text has reached the client we can no longer
        # restart cleanly, so a mid-stream failure just fails gracefully.
        for attempt_index, model_config in enumerate(candidates):
            provider = get_provider(model_config.provider)
            full_text = ""
            input_tokens = output_tokens = None
            is_last_candidate = attempt_index == len(candidates) - 1

            try:
                for chunk in provider.stream_chat(history, model_config.model_name, system_prompt=system_prompt):
                    if chunk.text:
                        full_text += chunk.text
                        yield _sse_event("message", chunk.text)
                    if chunk.done:
                        input_tokens, output_tokens = chunk.input_tokens, chunk.output_tokens
            except ProviderError as exc:
                if not full_text and not is_last_candidate:
                    continue
                # Log the real exception (Sentry, if configured) but never
                # show the raw upstream error to the user — it can contain
                # the model name or provider identity, which the portal is
                # meant to keep hidden (see spec section 1).
                capture_exception(exc)
                message.content = full_text or "(request failed before completion)"
                message.model_used = model_config
                message.save(update_fields=["content", "model_used"])
                yield _sse_event(
                    "error", "The assistant hit a problem generating a response. Please try again."
                )
                return

            message.content = full_text
            message.model_used = model_config
            message.input_tokens = input_tokens
            message.output_tokens = output_tokens
            message.estimated_cost = model_config.estimate_cost(input_tokens or 0, output_tokens or 0)
            message.save()
            yield _sse_event("done", "")
            return

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _sse_event(event_name, data):
    """Format one SSE event. `data` is HTML-escaped since htmx's sse-swap
    inserts it verbatim into the DOM, and multi-line data is split across
    multiple `data:` lines per the SSE spec (the client rejoins with \\n)."""
    escaped = escape(data) if data else ""
    lines = escaped.split("\n") if escaped else [""]
    payload = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event_name}\n{payload}\n\n"
