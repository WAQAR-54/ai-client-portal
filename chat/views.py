import logging
import re

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods
from sentry_sdk import capture_exception

from chat.models import Conversation, Message, MessageFeedback, ModelConfig, PromptTemplate
from chat.prompts import build_system_prompt
from chat.providers import ProviderError, get_provider
from chat.document_extraction import EXTRACTABLE_EXTENSIONS, extract_text, wrap_for_prompt
from chat.export import render_conversation_markdown, render_conversation_pdf, render_conversation_text
from chat.response_cache import get_cached_response, store_cached_response
from chat.router import NoModelAvailableError, classify_complexity, models_visible_to_user, select_model_candidates
from chat.utils import group_conversations
from governance.audit import log_action
from governance.features import require_feature
from governance.limits import UploadRejected, UsageLimitExceeded, check_usage_limits, get_usage_status, validate_upload

logger = logging.getLogger(__name__)


def _owned_conversation_or_404(request, conversation_id):
    return get_object_or_404(Conversation, id=conversation_id, user=request.user)


def _active_conversation_from_htmx_referrer(request):
    """Best-effort: which conversation the sidebar action was fired from,
    read from the `HX-Current-URL` header htmx sends automatically, so the
    re-rendered sidebar list keeps highlighting the right item."""
    current_url = request.headers.get("HX-Current-URL", "")
    match = re.search(r"/chat/conversations/(\d+)/", current_url)
    if not match:
        return None
    return Conversation.objects.filter(id=match.group(1), user=request.user).first()


@login_required
@require_GET
def download_attachment(request, conversation_id, message_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(Message, id=message_id, conversation=conversation)
    if not message.attachment:
        raise Http404
    return FileResponse(
        message.attachment.open("rb"),
        as_attachment=True,
        filename=message.attachment_original_name or message.attachment.name,
    )


@login_required
def chat_home(request, conversation_id=None):
    conversation = None
    if conversation_id:
        conversation = _owned_conversation_or_404(request, conversation_id)

    from governance.models import Plan
    from governance.plans import get_plan_status

    available_models = models_visible_to_user(request.user)
    plan_status = get_plan_status(request.user)
    upgrade_plan_choices = Plan.objects.filter(is_active=True, is_visible_to_admins=True).exclude(
        pk=plan_status["plan"].pk if plan_status["plan"] else None,
    )
    context = {
        **_conversation_list_context(request, conversation),
        "conversation": conversation,
        "messages_list": conversation.messages.all() if conversation else [],
        "has_models_available": available_models.exists(),
        "available_models": available_models,
        "model_rows": _model_catalog_rows(available_models, upgrade_plan_choices),
        "plan_status": plan_status,
        "usage": get_usage_status(request.user, conversation=conversation),
        "can_request_upgrade": bool(plan_status["plan"]),
        "upgrade_plan_choices": upgrade_plan_choices,
    }
    return render(request, "chat/chat_home.html", context)


def _model_catalog_rows(available_models, upgrade_plan_choices):
    """Every enabled model, annotated with whether the user's own plan
    already includes it. A model the user can't use yet is still shown
    (dimmed/locked in the template) together with the cheapest plan that
    *does* include it, pulled from Plan.allowed_models rather than any
    hardcoded model->plan mapping, so a plan edit in the admin dashboard
    is reflected here automatically."""
    all_enabled = ModelConfig.objects.filter(is_enabled=True).order_by("tier", "display_name")
    allowed_ids = set(available_models.values_list("id", flat=True))

    plans_by_model_id = {}
    for plan in upgrade_plan_choices.prefetch_related("allowed_models"):
        for model in plan.allowed_models.all():
            plans_by_model_id.setdefault(model.id, []).append(plan)

    rows = []
    for model in all_enabled:
        locked = model.id not in allowed_ids
        candidate_plans = plans_by_model_id.get(model.id, []) if locked else []
        rows.append(
            {
                "model": model,
                "locked": locked,
                "required_plan": candidate_plans[0] if candidate_plans else None,
            }
        )
    return rows


def _conversation_list_context(request, active_conversation=None):
    """Shared context for the sidebar list, used both on full page loads and
    on the pin/delete htmx partial re-renders."""
    own_conversations = Conversation.objects.filter(user=request.user)
    pinned = own_conversations.filter(is_pinned=True).order_by("-pinned_at")
    unpinned = own_conversations.filter(is_pinned=False).order_by("-updated_at")
    return {
        "pinned_conversations": pinned,
        "grouped_conversations": group_conversations(unpinned),
        "active_conversation_id": active_conversation.id if active_conversation else None,
    }


@login_required
@require_GET
def render_message(request, conversation_id, message_id):
    """Returns one message's final rendered bubble — used to swap a
    streamed reply's plain-text bubble for the Markdown-rendered version
    once the stream finishes (see _message_pending.html's sse:done hook)."""
    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(Message, id=message_id, conversation=conversation)
    # Always the newest message at the moment its stream finishes - safe to
    # treat as "last" so the Regenerate action becomes available on it.
    return render(request, "chat/_message_bubble.html", {"message": message, "is_last": True})


@login_required
@require_feature("upgrade_request")
@require_http_methods(["POST"])
def request_upgrade(request):
    from django.contrib import messages

    from governance.models import Plan, UpgradeRequest
    from governance.plans import get_assignment

    assignment = get_assignment(request.user)
    current_plan = assignment.plan if assignment else None

    requested_plan = None
    requested_plan_id = request.POST.get("requested_plan_id", "").strip()
    if requested_plan_id:
        requested_plan = (
            Plan.objects.filter(
                id=requested_plan_id,
                is_active=True,
                is_visible_to_admins=True,
            )
            .exclude(pk=current_plan.pk if current_plan else None)
            .first()
        )

    UpgradeRequest.objects.create(
        user=request.user,
        current_plan=current_plan,
        requested_plan=requested_plan,
        message=request.POST.get("message", "").strip(),
    )
    messages.success(request, _("Upgrade request sent — an admin will review it soon."))
    return redirect("chat:chat_home")


@login_required
@require_http_methods(["POST"])
def create_conversation(request):
    from django.contrib import messages

    from governance.limits import UsageLimitExceeded
    from governance.plans import check_session_creation_limit

    try:
        check_session_creation_limit(request.user)
    except UsageLimitExceeded as exc:
        messages.warning(request, str(exc))
        return redirect("chat:chat_home")

    conversation = Conversation.objects.create(user=request.user, title=_("New chat"))
    return redirect("chat:chat_conversation", conversation_id=conversation.id)


@login_required
@require_feature("conversation_pin_search")
@require_http_methods(["POST"])
def toggle_pin(request, conversation_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    conversation.is_pinned = not conversation.is_pinned
    conversation.pinned_at = timezone.now() if conversation.is_pinned else None
    conversation.save(update_fields=["is_pinned", "pinned_at"])
    return render(
        request,
        "chat/_conversation_list.html",
        _conversation_list_context(
            request,
            active_conversation=_active_conversation_from_htmx_referrer(request),
        ),
    )


@login_required
@require_http_methods(["POST"])
def delete_conversation(request, conversation_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    conversation.is_deleted = True
    conversation.deleted_at = timezone.now()
    conversation.save(update_fields=["is_deleted", "deleted_at"])
    log_action(
        actor=request.user,
        action_type="conversation.delete",
        target=conversation,
        old_value=conversation.title,
        new_value="",
    )

    current_url = request.headers.get("HX-Current-URL", "")
    deleted_conversation_path = reverse("chat:chat_conversation", kwargs={"conversation_id": conversation.id})
    if deleted_conversation_path in current_url:
        response = render(request, "chat/_conversation_list.html", _conversation_list_context(request))
        response["HX-Redirect"] = reverse("chat:chat_home")
        return response

    return render(
        request,
        "chat/_conversation_list.html",
        _conversation_list_context(
            request,
            active_conversation=_active_conversation_from_htmx_referrer(request),
        ),
    )


@login_required
@require_http_methods(["POST"])
def post_message(request, conversation_id):
    content = request.POST.get("content", "").strip()
    uploaded_file = request.FILES.get("attachment")
    if not content and not uploaded_file:
        return render(
            request,
            "chat/_limit_exceeded.html",
            {"message": _("Type a message or attach a file first.")},
            status=400,
        )

    if uploaded_file:
        from governance.plans import has_feature

        if not has_feature(request.user, "file_upload"):
            return render(
                request,
                "chat/_limit_exceeded.html",
                {"message": _("File upload isn't included in your current plan.")},
                status=403,
            )
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
            Conversation.objects.select_for_update(),
            id=conversation_id,
            user=request.user,
        )
        try:
            check_usage_limits(request.user, conversation)
        except UsageLimitExceeded as exc:
            return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=429)

        Conversation.objects.filter(pk=conversation.pk).update(updated_at=timezone.now())
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
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="",
        )

    return render(
        request,
        "chat/_message_pending.html",
        {
            "conversation": conversation,
            "pending_message": pending_assistant_message,
            "user_message": user_message,
            "model_id": model_id,
        },
    )


@login_required
@require_http_methods(["POST"])
def edit_message(request, conversation_id, message_id):
    """Editing a user message regenerates the conversation forward from that
    point - matches ChatGPT/Claude: the original message and every reply
    that came after it are discarded, not kept as a branch (confirmed with
    the user rather than guessed, since the alternative - versioned
    branches - needs a real data model change)."""
    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(Message, id=message_id, conversation=conversation, role=Message.Role.USER)
    content = request.POST.get("content", "").strip()
    if not content:
        return render(request, "chat/_limit_exceeded.html", {"message": _("Type a message first.")}, status=400)

    with transaction.atomic():
        conversation = get_object_or_404(
            Conversation.objects.select_for_update(), id=conversation_id, user=request.user
        )
        try:
            check_usage_limits(request.user, conversation)
        except UsageLimitExceeded as exc:
            return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=429)

        Message.objects.filter(conversation=conversation, id__gte=message.id).delete()
        Conversation.objects.filter(pk=conversation.pk).update(updated_at=timezone.now())
        new_user_message = Message.objects.create(conversation=conversation, role=Message.Role.USER, content=content)
        pending_assistant_message = Message.objects.create(
            conversation=conversation, role=Message.Role.ASSISTANT, content=""
        )

    prior_messages = list(conversation.messages.exclude(id__in=[new_user_message.id, pending_assistant_message.id]))
    return render(
        request,
        "chat/_conversation_messages.html",
        {
            "prior_messages": prior_messages,
            "conversation": conversation,
            "pending_message": pending_assistant_message,
            "user_message": new_user_message,
            "model_id": "",
        },
    )


@login_required
@require_http_methods(["POST"])
def regenerate_message(request, conversation_id, message_id):
    """Regenerating replaces this exact reply in place (same row/id reset
    back to pending) rather than deleting/recreating it - confirmed with the
    user as the intended behavior, and it means messages that came after
    this one (if any) are left untouched instead of needing to be
    truncated too."""
    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(Message, id=message_id, conversation=conversation, role=Message.Role.ASSISTANT)

    try:
        check_usage_limits(request.user, conversation)
    except UsageLimitExceeded as exc:
        return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=429)

    message.content = ""
    message.model_used = None
    message.input_tokens = None
    message.output_tokens = None
    message.estimated_cost = None
    message.save(update_fields=["content", "model_used", "input_tokens", "output_tokens", "estimated_cost"])

    return render(
        request,
        "chat/_pending_assistant_row.html",
        {"conversation": conversation, "pending_message": message, "model_id": ""},
    )


@login_required
@require_http_methods(["POST"])
def submit_feedback(request, conversation_id, message_id):
    """Thumbs up/down on an assistant reply, plus an optional follow-up
    comment on a thumbs-down. Distinguishing the two is based on whether
    `comment` was posted at all (a rating click never includes it; the
    follow-up comment form always does, even if left empty) rather than on
    `rating`, since the comment form doesn't need to resend it."""
    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(Message, id=message_id, conversation=conversation, role=Message.Role.ASSISTANT)
    existing = MessageFeedback.objects.filter(message=message).first()

    comment = request.POST.get("comment")
    if comment is None:
        rating = request.POST.get("rating", "").strip()
        if rating not in MessageFeedback.Rating.values:
            return HttpResponseBadRequest("Invalid rating")
        if existing and existing.rating == rating:
            existing.delete()
            feedback = None
        else:
            feedback, _ = MessageFeedback.objects.update_or_create(
                message=message,
                defaults={"user": request.user, "rating": rating, "model_used": message.model_used},
            )
    else:
        if not existing:
            return HttpResponseBadRequest("Rate the message before adding a comment")
        existing.comment = comment.strip()[:500]
        existing.save(update_fields=["comment", "updated_at"])
        feedback = existing

    return render(request, "chat/_message_feedback.html", {"message": message, "feedback": feedback})


@login_required
@require_GET
def export_conversation_markdown(request, conversation_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    body = render_conversation_markdown(conversation)
    response = HttpResponse(body, content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{_export_filename(conversation)}.md"'
    return response


@login_required
@require_GET
def export_conversation_text(request, conversation_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    body = render_conversation_text(conversation)
    response = HttpResponse(body, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{_export_filename(conversation)}.txt"'
    return response


@login_required
@require_GET
def export_conversation_pdf(request, conversation_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    pdf_bytes = render_conversation_pdf(conversation)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{_export_filename(conversation)}.pdf"'
    return response


def _export_filename(conversation):
    slug = re.sub(r"[^a-z0-9]+", "-", conversation.title.lower()).strip("-") or "conversation"
    return slug[:60]


def _visible_prompt_templates(user):
    """Personal templates + this user's department's team templates, if any."""
    from django.db.models import Q

    filters = Q(owner=user)
    if user.department_id:
        filters |= Q(department_id=user.department_id)
    return PromptTemplate.objects.filter(filters)


@login_required
@require_feature("prompt_templates")
@require_GET
def prompt_template_list(request):
    return render(request, "chat/_prompt_template_picker.html", {"templates": _visible_prompt_templates(request.user)})


@login_required
@require_feature("prompt_templates")
@require_http_methods(["POST"])
def save_prompt_template(request):
    name = request.POST.get("name", "").strip()
    content = request.POST.get("content", "").strip()
    if not name or not content:
        return render(
            request, "chat/_limit_exceeded.html", {"message": _("A template needs both a name and text.")}, status=400
        )
    PromptTemplate.objects.create(owner=request.user, name=name[:100], content=content)
    return render(request, "chat/_prompt_template_picker.html", {"templates": _visible_prompt_templates(request.user)})


@login_required
@require_feature("prompt_templates")
@require_http_methods(["POST"])
def delete_prompt_template(request, template_id):
    # Owner-only - a department template has owner=None and can never
    # match here, so this can't be used to delete a team template.
    get_object_or_404(PromptTemplate, id=template_id, owner=request.user).delete()
    return render(request, "chat/_prompt_template_picker.html", {"templates": _visible_prompt_templates(request.user)})


def _notify_if_usage_warning(user):
    """Fires the in-app+email "approaching a limit" notice the first time
    a user crosses 80% of any cap after a message - deduped to at most
    once per 24h per user so it doesn't re-fire on every message once
    already over the threshold."""
    from datetime import timedelta

    from django.utils import timezone

    from notifications.models import NotificationType
    from notifications.notify import notify, recently_notified

    usage = get_usage_status(user)
    if not usage["warn"]:
        return
    if recently_notified(user, NotificationType.USAGE_WARNING, since=timezone.now() - timedelta(hours=24)):
        return

    worst = max(usage["metrics"], key=lambda m: m["pct"])
    notify(
        user,
        NotificationType.USAGE_WARNING,
        title="You're approaching a usage limit",
        body=f"{worst['label']}: {worst['pct']}% used. Contact your administrator if you need more.",
    )


def _history_with_attachments(conversation, exclude_message_id):
    """Message history as provider-ready dicts, with the text content of
    any attachment we can extract from (txt/csv/md/json/pdf/docx/xlsx)
    appended inline, delimited via document_extraction.wrap_for_prompt()
    so the model treats it as reference material, never instructions (see
    that module's docstring and the system prompt in chat/prompts.py -
    this is the other required half of the same defense). Other file
    types (images) are stored and shown in the UI but not read by the
    model yet."""
    history = []
    messages = conversation.messages.exclude(id=exclude_message_id).order_by("created_at")
    for msg in messages:
        content = msg.content
        if msg.attachment:
            name = msg.attachment_original_name
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            extracted = extract_text(msg.attachment, extension) if extension in EXTRACTABLE_EXTENSIONS else None
            if extracted is not None:
                content = f"{content}\n\n{wrap_for_prompt(name, extracted)}"
            else:
                content = f"{content}\n\n[Attached file: {name} (not readable by the assistant yet)]"
        history.append({"role": msg.role, "content": content})
    return history


@login_required
@require_GET
def stream_message(request, conversation_id, message_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(
        Message,
        id=message_id,
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content="",
    )

    history = _history_with_attachments(conversation, exclude_message_id=message.id)
    requested_model_id = request.GET.get("model_id", "").strip()

    def event_stream():
        # Every exit path below saves *something* to message.content and
        # then yields "done" — never a separate "error" event. An SSE event
        # literally named "error" collides with EventSource's own reserved
        # connection-error event and silently never reaches sse-swap
        # listeners (confirmed by hand: htmx never applied the swap even
        # though the raw SSE bytes were well-formed). Routing every outcome
        # through the same "done" -> render_message round trip sidesteps
        # that entirely and means there's only one client-side mechanism
        # to get right, not two.
        try:
            if requested_model_id and models_visible_to_user(request.user).filter(id=requested_model_id).exists():
                candidates = [ModelConfig.objects.get(id=requested_model_id)]
            else:
                tier = classify_complexity(history[-1]["content"] if history else "")
                candidates = select_model_candidates(request.user, tier)
        except NoModelAvailableError as exc:
            message.content = str(exc)
            message.save(update_fields=["content"])
            yield _sse_event("done", "")
            return
        if not candidates:
            message.content = "No AI model is enabled and permitted for this user."
            message.save(update_fields=["content"])
            yield _sse_event("done", "")
            return

        system_prompt = build_system_prompt(request.user)

        from governance.plans import validate_context_tokens

        try:
            validate_context_tokens(request.user, system_prompt, history)
        except UsageLimitExceeded as exc:
            message.content = str(exc)
            message.save(update_fields=["content"])
            yield _sse_event("done", "")
            return

        # Exact-match cache: only ever checked against candidates[0] (the
        # model this request would actually use first), keyed on the full
        # history so a repeat of the identical exchange - not just the
        # same trailing message - is what's required to hit. See
        # chat/response_cache.py for why the whole history is hashed.
        cached = get_cached_response(request.user.id, candidates[0].id, system_prompt, history)
        if cached is not None:
            yield _sse_event("message", cached["text"])
            message.content = cached["text"]
            message.model_used = candidates[0]
            message.input_tokens = cached["input_tokens"]
            message.output_tokens = cached["output_tokens"]
            message.estimated_cost = candidates[0].estimate_cost(
                cached["input_tokens"] or 0, cached["output_tokens"] or 0
            )
            message.served_from_cache = True
            message.save()
            _notify_if_usage_warning(request.user)
            yield _sse_event("done", "")
            return

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
                # Log the real exception (console/file always, Sentry too if
                # configured) but never show the raw upstream error to the
                # user — it can contain the model name or provider identity,
                # which the portal is meant to keep hidden (see spec section 1).
                logger.exception(
                    "AI provider call failed (provider=%s, model=%s)",
                    model_config.provider,
                    model_config.model_name,
                )
                capture_exception(exc)
                message.content = full_text or "The assistant hit a problem generating a response. Please try again."
                message.model_used = model_config
                message.save(update_fields=["content", "model_used"])
                yield _sse_event("done", "")
                return

            message.content = full_text
            message.model_used = model_config
            message.input_tokens = input_tokens
            message.output_tokens = output_tokens
            message.estimated_cost = model_config.estimate_cost(input_tokens or 0, output_tokens or 0)
            message.save()
            store_cached_response(
                request.user.id,
                model_config.id,
                system_prompt,
                history,
                text=full_text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            _notify_if_usage_warning(request.user)
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
