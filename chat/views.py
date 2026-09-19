import logging
import re
from datetime import timedelta
from urllib.parse import quote, urlencode

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.html import escape
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods
from sentry_sdk import capture_exception, new_scope

from chat.models import ArenaComparison, Conversation, Message, MessageFeedback, Project, PromptTemplate
from chat.prompts import build_system_prompt
from chat.providers import ProviderError, get_provider
from chat.document_extraction import (
    EXTRACTABLE_EXTENSIONS,
    IMAGE_EXTENSIONS,
    extract_image,
    extract_text,
    wrap_for_prompt,
)
from chat.export import render_conversation_markdown, render_conversation_pdf, render_conversation_text
from chat.response_cache import get_cached_response, store_cached_response
from chat.router import (
    NoModelAvailableError,
    classify_complexity,
    looks_like_document_request,
    match_routing_rule,
    models_visible_to_user,
    select_model_candidates,
)
from chat import live_intelligence
from chat.utils import age_label, group_conversations
from governance.audit import log_action
from governance.features import require_feature, user_has_feature
from governance.limits import (
    UploadRejected,
    UsageLimitExceeded,
    check_attachment_monthly_limit,
    check_media_generation_monthly_limit,
    check_research_monthly_limit,
    check_usage_limits,
    get_usage_status,
    validate_upload,
)
from providers.models import Provider, ProviderModel

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

    from chat.prompts import AGENT_PERSONAS
    from governance.models import Plan
    from governance.plans import get_budget_automation_status, get_plan_status, get_request_count_status, has_feature

    available_models = models_visible_to_user(request.user)
    # Sidebar provider filter tabs - deliberately reuses available_models
    # (already models_visible_to_user's RBAC/region/plan-filtered result)
    # rather than Provider.objects.filter(is_connected=True) directly, so a
    # tab for a provider this user isn't actually allowed to use can never
    # appear.
    sidebar_providers = Provider.objects.filter(id__in=available_models.values_list("provider_id", flat=True)).order_by(
        "name"
    )
    plan_status = get_plan_status(request.user)
    upgrade_plan_choices = Plan.objects.filter(is_active=True, is_visible_to_admins=True).exclude(
        pk=plan_status["plan"].pk if plan_status["plan"] else None,
    )
    context = {
        **_conversation_list_context(request, conversation),
        "conversation": conversation,
        "messages_list": _messages_with_switch_dividers(conversation),
        "has_models_available": available_models.exists(),
        "available_models": available_models,
        "sidebar_providers": sidebar_providers,
        "model_rows": _model_catalog_rows(available_models, upgrade_plan_choices),
        "plan_status": plan_status,
        "budget_automation": get_budget_automation_status(request.user),
        "usage": get_usage_status(request.user, conversation=conversation),
        "request_count": get_request_count_status(request.user, conversation=conversation),
        "can_select_model": has_feature(request.user, "model_selection"),
        "can_use_research": has_feature(request.user, "research"),
        "can_generate_media": has_feature(request.user, "media_generation"),
        "can_generate_document": has_feature(request.user, "document_generation"),
        "can_use_agent_mode": has_feature(request.user, "agent_mode"),
        "agent_personas": [(key, label) for key, (label, _instruction) in AGENT_PERSONAS.items()],
        "can_request_upgrade": bool(plan_status["plan"]),
        "upgrade_plan_choices": upgrade_plan_choices,
        # Only the flags - the headlines themselves load separately (see
        # live_intelligence_feed), so /chat/ never waits on a news source.
        "live_intelligence_enabled": live_intelligence.enabled(),
        "live_commands": [(key, label) for key, label in live_intelligence.COMMANDS],
    }
    return render(request, "chat/chat_home.html", context)


def _messages_with_switch_dividers(conversation):
    """A conversation's messages as a plain list, each assistant message
    annotated with .show_switch_divider - True when it answered with a
    different model than the assistant message before it, False for the
    conversation's first assistant reply (nothing to have "switched" from
    yet) and for every user message. Computed once here, in a real list
    (not the lazy queryset), so the loop in chat_home.html can just check
    the attribute instead of re-deriving it - and so a single-message htmx
    response (post_message/regenerate/edit, which renders _message_bubble.html
    standalone) safely has no such attribute and the template's
    {% if message.show_switch_divider %} just resolves falsy for it.

    Also annotates Compare-mode (ArenaComparison) messages so the template
    can render each pair as one side-by-side block instead of two separate
    linear bubbles: the pair's first response gets .arena_comparison (render
    chat/_arena_pair.html for it), its second response gets .arena_skip
    (already rendered as part of that pair, skip it here). Neither
    contributes to the switch-divider tracking above - two models answering
    side by side isn't a "switch", and resuming normal chat afterward should
    still compare against the last *single-model* reply, not either arena
    side."""
    if not conversation:
        return []
    messages = list(conversation.messages.all())
    arena_by_response_id = {}
    for comparison in conversation.arena_comparisons.select_related(
        "model_a__provider", "model_b__provider", "response_a", "response_b"
    ):
        arena_by_response_id[comparison.response_a_id] = comparison
        arena_by_response_id[comparison.response_b_id] = comparison

    previous_label = None
    for message in messages:
        comparison = arena_by_response_id.get(message.id)
        if comparison is not None:
            if message.id == comparison.response_a_id:
                message.arena_comparison = comparison
            else:
                message.arena_skip = True
            continue
        if message.role != Message.Role.ASSISTANT:
            continue
        label = message.model_label
        message.show_switch_divider = previous_label is not None and label != previous_label
        previous_label = label
    return messages


def _model_catalog_rows(available_models, upgrade_plan_choices):
    """Every enabled model, annotated with whether the user's own plan
    already includes it. A model the user can't use yet is still shown
    (dimmed/locked in the template) together with the cheapest plan that
    *does* include it, pulled from Plan.allowed_models rather than any
    hardcoded model->plan mapping, so a plan edit in the admin dashboard
    is reflected here automatically."""
    all_enabled = (
        ProviderModel.objects.filter(is_enabled=True)
        .select_related("provider")
        .order_by("provider__name", "tier", "display_name")
    )
    allowed_ids = set(available_models.values_list("id", flat=True))

    plans_by_model_id = {}
    for plan in upgrade_plan_choices.prefetch_related("allowed_provider_models"):
        for model in plan.allowed_provider_models.all():
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


# Sidebar was unbounded - a real gap found in the production-readiness
# audit (measured: page size/query cost grows without limit as a user's
# conversation history grows). Pinned conversations stay unbounded (in
# practice always few - bounded by how many a person will realistically
# pin, not by total history), only the day-grouped unpinned list is capped.
# The client-side search/filter JS (chat_home.html) can only ever see
# conversations actually in the DOM, so search is intentionally NOT capped
# here - see search_conversations below, which the search box now hits
# directly instead of relying on pure client-side filtering.
CONVERSATIONS_PAGE_SIZE = 100


def _encode_conversation_cursor(conversation):
    return f"{conversation.updated_at.isoformat()}|{conversation.id}"


def _decode_conversation_cursor(raw):
    """Returns (updated_at, id) or None for a missing/malformed cursor -
    callers treat None as "start from the top", never as an error, since a
    stale/tampered cursor from an old page load must degrade gracefully
    rather than 400 or crash."""
    from django.utils.dateparse import parse_datetime

    updated_at_str, _, id_str = (raw or "").partition("|")
    updated_at = parse_datetime(updated_at_str) if updated_at_str else None
    if updated_at is None or not id_str.isdigit():
        return None
    return updated_at, int(id_str)


def _unpinned_conversations_page(request, *, before_cursor=None):
    """One page of unpinned conversations, ordered newest-first with a
    stable tiebreaker (id) so pagination can't skip/duplicate a row when
    two conversations share the exact same updated_at - a real risk with
    auto_now timestamps under concurrent activity, and explicitly called
    out in the audit ("pagination must have stable ordering"). Returns
    (page, has_more) - page is at most CONVERSATIONS_PAGE_SIZE long."""
    qs = (
        Conversation.objects.filter(user=request.user, is_pinned=False)
        .select_related("last_provider_model__provider", "project")
        .order_by("-updated_at", "-id")
    )
    if before_cursor is not None:
        updated_at, conv_id = before_cursor
        qs = qs.filter(Q(updated_at__lt=updated_at) | Q(updated_at=updated_at, id__lt=conv_id))
    rows = list(qs[: CONVERSATIONS_PAGE_SIZE + 1])
    has_more = len(rows) > CONVERSATIONS_PAGE_SIZE
    return rows[:CONVERSATIONS_PAGE_SIZE], has_more


def _load_more_url(cursor, last_label, active_conversation_id):
    """Built server-side (not composed piecemeal in the template) so query
    values that could contain '&'/'?' - a conversation's day-bucket label
    is user-facing translated text, and a title-driven label could in
    principle contain odd characters - are properly encoded, not just
    concatenated into an href."""
    params = {"cursor": cursor, "last_label": last_label or ""}
    if active_conversation_id:
        params["active_conversation_id"] = active_conversation_id
    return f"{reverse('chat:load_more_conversations')}?{urlencode(params)}"


def _conversation_list_context(request, active_conversation=None):
    """Shared context for the sidebar list, used both on full page loads and
    on the pin/delete/project htmx partial re-renders. Always starts from
    the first page - toggle_pin/delete_conversation/project actions re-
    render this from scratch, same as a fresh page load; a user who had
    already clicked "Load more" simply sees the list collapse back to page
    one, rather than this trying to remember how many pages were open."""
    own_conversations = Conversation.objects.filter(user=request.user).select_related(
        "last_provider_model__provider", "project"
    )
    pinned = own_conversations.filter(is_pinned=True).order_by("-pinned_at")
    unpinned, has_more = _unpinned_conversations_page(request)
    grouped = group_conversations(unpinned)
    # Personal projects (see chat.models.Project) - annotated count uses the
    # reverse FK at the SQL level (Count/filter), so it stays correct
    # regardless of which manager touches Conversation elsewhere (the
    # is_deleted=False filter here matches ActiveConversationManager's own
    # default exactly, rather than relying on it).
    user_projects = Project.objects.filter(user=request.user).annotate(
        conversation_count=Count("conversations", filter=Q(conversations__is_deleted=False))
    )
    active_id = active_conversation.id if active_conversation else None
    return {
        "pinned_conversations": pinned,
        "grouped_conversations": grouped,
        "active_conversation_id": active_id,
        "user_projects": user_projects,
        "has_more_conversations": has_more,
        "load_more_url": (
            _load_more_url(_encode_conversation_cursor(unpinned[-1]), grouped[-1][0], active_id) if has_more else ""
        ),
    }


@login_required
@require_GET
def load_more_conversations(request):
    """ "Load more" click at the bottom of the sidebar list - appends the
    next page after the cursor rather than re-rendering everything (that
    would both re-fetch conversations the browser already has and reset
    the user's scroll position). A malformed/stale cursor just restarts
    from the top rather than erroring - see _decode_conversation_cursor."""
    cursor = _decode_conversation_cursor(request.GET.get("cursor", ""))
    active_id = request.GET.get("active_conversation_id") or None
    batch, has_more = _unpinned_conversations_page(request, before_cursor=cursor)
    grouped = group_conversations(batch)
    # Avoid a duplicate "Previous 30 Days" (etc.) heading when this batch
    # continues the same bucket the previous page ended on - cosmetic, but
    # a repeated heading mid-list reads as a bug even though nothing is
    # actually wrong with the data underneath it.
    continues_previous_bucket = bool(grouped) and grouped[0][0] == request.GET.get("last_label", "")
    return render(
        request,
        "chat/_conversation_list_more.html",
        {
            "grouped_conversations": grouped,
            "continues_previous_bucket": continues_previous_bucket,
            "active_conversation_id": active_id,
            "has_more_conversations": has_more,
            "load_more_url": (
                _load_more_url(_encode_conversation_cursor(batch[-1]), grouped[-1][0], active_id) if has_more else ""
            ),
        },
    )


@login_required
@require_GET
def search_conversations(request):
    """Server-side search backing the sidebar's search box - added
    alongside the pagination above specifically so search still reaches a
    user's FULL conversation history, not just whatever page happens to be
    loaded in the DOM (the audit's own explicit requirement: "search must
    still work"). An empty query just returns the normal first page
    (unchanged behavior) so clearing the search box restores pagination.

    Capped at CONVERSATIONS_PAGE_SIZE * 2 results, generous for an actual
    search (which narrows, rather than lists, history) without letting a
    single-character query force-render someone's entire history in one
    response - a "refine your search" hint appears if the cap was hit,
    rather than silently truncating with no explanation. Provider/project
    filtering stays client-side (chat_home.html's applyFilter()), applied
    to whatever this renders, exactly as it already does for the normal
    paginated view."""
    query = request.GET.get("q", "").strip()
    if not query:
        return render(request, "chat/_conversation_list.html", _conversation_list_context(request))

    matches = list(
        Conversation.objects.filter(user=request.user, title__icontains=query)
        .select_related("last_provider_model__provider", "project")
        .order_by("-updated_at", "-id")[: CONVERSATIONS_PAGE_SIZE * 2 + 1]
    )
    truncated = len(matches) > CONVERSATIONS_PAGE_SIZE * 2
    matches = matches[: CONVERSATIONS_PAGE_SIZE * 2]
    return render(
        request,
        "chat/_conversation_search_results.html",
        {
            "grouped_conversations": group_conversations(matches),
            "active_conversation_id": None,
            "truncated": truncated,
            "query": query,
        },
    )


# Categories shown as cards on the chat home page. Deliberately three: the
# home page is an AI chat workspace first, not a news site - developer news
# and GitHub trends are still one click away via the quick commands.
INTEL_CARD_KEYS = ("technology", "ai", "security")
INTEL_HEADLINES = 4
INTEL_REFRESH_COOLDOWN_SECONDS = 60


def _intel_card(key, result):
    meta = live_intelligence.CATEGORIES[key]
    stories = result["stories"]
    return {
        "key": key,
        "label": meta["label"],
        "blurb": meta["blurb"],
        "state": result["state"],
        "count": len(stories),
        "age": age_label(result["fetched_at"]),
    }


def _intel_context(force=False):
    """Everything the Live Intelligence fragment renders. Each card is a
    separate category result, so one failing source set degrades only its own
    card. `headlines` are the newest stories of the first card's category."""
    results = {key: live_intelligence.get_category(key, force=force) for key in INTEL_CARD_KEYS}
    cards = [_intel_card(key, results[key]) for key in INTEL_CARD_KEYS]
    first = results[INTEL_CARD_KEYS[0]]
    headlines = [{**story, "age": age_label(story["published"])} for story in first["stories"][:INTEL_HEADLINES]]
    states = {c["state"] for c in cards}
    if not live_intelligence.enabled():
        overall = "disabled"
    elif "live" in states:
        overall = "live"
    elif "cached" in states:
        overall = "cached"
    elif "stale" in states:
        overall = "stale"
    elif states == {"empty"}:
        overall = "empty"
    else:
        overall = "unavailable"
    fetched = [r["fetched_at"] for r in results.values() if r["fetched_at"]]
    return {
        "cards": cards,
        "headlines": headlines,
        "headlines_title": live_intelligence.CATEGORIES[INTEL_CARD_KEYS[0]]["title"],
        "overall": overall,
        "overall_age": age_label(min(fetched)) if fetched else "",
        "commands": live_intelligence.COMMANDS,
    }


@login_required
@require_feature("live_intelligence")
@require_GET
def live_intelligence_feed(request):
    """The home page's Live Intelligence content, loaded by htmx AFTER the
    page has rendered (see _live_intelligence_section.html) - so /chat/
    itself never waits on, or can be broken by, a news source. Served from
    cache when fresh; a cold cache fetches the few feeds in parallel with
    short timeouts, and every failure mode renders a message, not an error."""
    return render(request, "chat/_live_intelligence_feed.html", _intel_context())


@login_required
@require_feature("live_intelligence")
@require_http_methods(["POST"])
def live_intelligence_refresh(request):
    """ "Refresh intelligence". Only claims "updated" if it actually refetched:
    limited to one real refresh per user per minute (cache.add is atomic), so
    a click-happy user can't turn this into outbound request spam, and a
    refresh inside the cooldown says so instead of pretending."""
    try:
        allowed = cache.add(f"liveintel:refresh:{request.user.id}", 1, INTEL_REFRESH_COOLDOWN_SECONDS)
    except Exception:
        allowed = False
    context = _intel_context(force=allowed)
    context["refresh_note"] = "" if allowed else _("Already refreshed moments ago - try again in a minute.")
    return render(request, "chat/_live_intelligence_feed.html", context)


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
@require_GET
def artifact_panel(request, conversation_id, message_id):
    """Content for the artifact side panel's body - loaded via htmx when a
    message's doc card (_message_bubble.html) is clicked. 404s for a
    non-artifact message so this can never be used to peek at a plain
    reply's content through a different URL than render_message already
    allows for."""
    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(
        Message, id=message_id, conversation=conversation, role=Message.Role.ASSISTANT, is_artifact=True
    )
    from governance.plans import has_feature

    return render(
        request,
        "chat/_artifact_panel_content.html",
        {"message": message, "can_generate_document": has_feature(request.user, "document_generation")},
    )


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

    # So "New chat" from inside a selected project lands already grouped -
    # re-validated against this user's own projects rather than trusted
    # from the POST, same reasoning as every other POSTed id in this file.
    project_id = request.POST.get("project_id", "").strip()
    project = Project.objects.filter(id=project_id, user=request.user).first() if project_id else None

    conversation = Conversation.objects.create(user=request.user, title=_("New conversation"), project=project)
    url = reverse("chat:chat_conversation", kwargs={"conversation_id": conversation.id})
    starter_text = request.POST.get("starter_text", "").strip()
    # A Live Intelligence quick command (see live_intelligence.COMMANDS):
    # whitelisted here and re-validated again in post_message - never trusted
    # from the URL it round-trips through. Ignored (plain starter) when the
    # feature is off, so a command can never send an ungrounded "today's
    # news" question that the model would answer from memory.
    intel = request.POST.get("intel", "").strip()
    intel_allowed = (
        intel in live_intelligence.VALID_KEYS
        and live_intelligence.enabled()
        and user_has_feature(request.user, "live_intelligence")
    )
    if intel_allowed:
        starter_text = live_intelligence.prompt_for(intel)
    if starter_text:
        url = f"{url}?starter={quote(starter_text)}"
        if intel_allowed:
            url = f"{url}&intel={quote(intel)}"
    elif request.POST.get("compare") == "1":
        # "Compare" from the chat home: open the new conversation straight on
        # the pick-two-models screen (the screen only exists inside a
        # conversation, so from the home it used to do nothing at all).
        url = f"{url}?compare=1"
    return redirect(url)


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


def _conv_list_and_projects_response(request):
    """Shared by every Project CRUD view below: the conv list re-render
    (in-band, matching toggle_pin/delete_conversation's own hx-target) plus
    the sidebar's Projects section as an htmx out-of-band swap - project
    create/rename/delete/move can all change what either partial shows
    (a new project, a renamed one, a changed per-project conversation
    count), so both always refresh together rather than drifting stale
    until the next full page load."""
    from django.template.loader import render_to_string

    context = _conversation_list_context(request, active_conversation=_active_conversation_from_htmx_referrer(request))
    html = render_to_string("chat/_conversation_list.html", context, request=request)
    html += render_to_string("chat/_projects_section.html", context, request=request)
    return HttpResponse(html)


@login_required
@require_feature("projects")
@require_http_methods(["POST"])
def create_project(request):
    name = request.POST.get("name", "").strip()
    if not name:
        return HttpResponseBadRequest("Project name is required")
    Project.objects.create(user=request.user, name=name[:100])
    return _conv_list_and_projects_response(request)


@login_required
@require_feature("projects")
@require_http_methods(["POST"])
def rename_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, user=request.user)
    name = request.POST.get("name", "").strip()
    if not name:
        return HttpResponseBadRequest("Project name is required")
    project.name = name[:100]
    project.save(update_fields=["name"])
    return _conv_list_and_projects_response(request)


@login_required
@require_feature("projects")
@require_http_methods(["POST"])
def delete_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, user=request.user)
    project.delete()  # SET_NULL on Conversation.project - never deletes the conversations themselves
    return _conv_list_and_projects_response(request)


@login_required
@require_feature("projects")
@require_http_methods(["POST"])
def move_conversation_to_project(request, conversation_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    project_id = request.POST.get("project_id", "").strip()
    # "" (Remove from project) is a valid, deliberate choice, not a missing
    # param - re-validated against this user's own projects either way,
    # same reasoning as every other POSTed id in this file.
    project = Project.objects.filter(id=project_id, user=request.user).first() if project_id else None
    conversation.project = project
    conversation.save(update_fields=["project"])
    return _conv_list_and_projects_response(request)


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

    if content:
        from governance.pii import PIIBlocked, apply_pii_rules

        try:
            content = apply_pii_rules(content)
        except PIIBlocked as exc:
            return render(
                request,
                "chat/_limit_exceeded.html",
                {
                    "message": _("This message appears to contain %(kind)s and can't be sent.")
                    % {"kind": exc.kind_label}
                },
                status=400,
            )

        from governance.limits import UsageLimitExceeded
        from governance.plans import check_message_length_limit

        try:
            check_message_length_limit(request.user, content)
        except UsageLimitExceeded as exc:
            return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=400)

    attachment_kind = ""
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

        extension = uploaded_file.name.rsplit(".", 1)[-1].lower() if "." in uploaded_file.name else ""
        attachment_kind = "image" if extension in IMAGE_EXTENSIONS else "document"
        try:
            check_attachment_monthly_limit(request.user, attachment_kind)
        except UploadRejected as exc:
            return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=400)

    # Only offer a manually-picked model if it's actually one this user is
    # currently allowed to use — otherwise silently fall back to auto-routing
    # rather than trusting a stale/tampered value from the form. Same
    # reasoning for the plan-level model_selection flag: the dropdown is
    # already hidden client-side when a plan disallows it (see chat_home's
    # can_select_model), but a POSTed model_id shouldn't be trusted just
    # because the UI that would normally set it wasn't shown.
    from governance.plans import has_feature

    model_id = request.POST.get("model_id", "").strip()
    if model_id and not has_feature(request.user, "model_selection"):
        model_id = ""
    if model_id and not models_visible_to_user(request.user).filter(id=model_id).exists():
        model_id = ""

    # Same reasoning as model_id just above - the toggle is already hidden
    # client-side when the plan disallows it (chat_home's can_use_research),
    # but a POSTed "research=on" shouldn't be trusted just because the UI
    # that would normally set it wasn't shown.
    research = request.POST.get("research") == "on" and has_feature(request.user, "research")
    # Live Intelligence quick command - same validation as create_conversation;
    # stored on the pending assistant row (not just the stream URL) so
    # Regenerate stays grounded. See chat.models.Message.live_intel.
    live_intel = request.POST.get("live_intel", "").strip()
    if not (
        live_intel in live_intelligence.VALID_KEYS
        and live_intelligence.enabled()
        and user_has_feature(request.user, "live_intelligence")
    ):
        live_intel = ""
    if research:
        try:
            check_research_monthly_limit(request.user)
        except UploadRejected as exc:
            return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=429)

    # Same reasoning again - AGENT_PERSONAS.get() below only ever accepts
    # one of the 3 known keys anyway, but the feature gate still has to be
    # re-checked server-side rather than trusted from a hidden POST field.
    from chat.prompts import AGENT_PERSONAS

    agent_persona = request.POST.get("agent_persona", "").strip()
    if agent_persona not in AGENT_PERSONAS or not has_feature(request.user, "agent_mode"):
        agent_persona = ""

    # Composer's "Code" output-mode toggle (see chat/prompts.py's own
    # comment on CODE_OUTPUT_HINT) - no feature gate to re-check here,
    # unlike research/agent_persona/model_id above, since it's available
    # to everyone regardless of plan (a one-off prompt hint, not a new
    # capability). Still whitelisted against the one known value rather
    # than trusting an arbitrary POSTed string straight into the prompt.
    output_mode = request.POST.get("output_mode", "").strip()
    if output_mode != "code":
        output_mode = ""

    # Document mode - gated on the existing document_generation Plan
    # feature flag (already used for the per-message export-menu Download
    # action). Auto-detected from the message content by default (see
    # chat/router.py::looks_like_document_request) rather than requiring
    # the user to find and click a composer toggle first - real usage
    # showed people simply typing "write me a proposal" and getting a
    # confused refusal because the toggle was never touched. The explicit
    # POST field is kept as a manual override (e.g. a future "force
    # document mode" affordance), not currently exposed in the composer UI.
    document_mode = has_feature(request.user, "document_generation") and (
        request.POST.get("document_mode") == "on" or looks_like_document_request(content)
    )

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
            user_message.attachment_kind = attachment_kind
            user_message.save(
                update_fields=["attachment", "attachment_original_name", "attachment_size", "attachment_kind"]
            )

        if conversation.title == "New conversation":
            conversation.title = (content or uploaded_file.name)[:60]
            conversation.save(update_fields=["title"])

        pending_assistant_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="",
            used_research=research,
            live_intel=live_intel,
            is_artifact=document_mode,
            # Provisional - overwritten in stream_message once the reply's
            # own "# Heading" is known (see extract_document_title). Just a
            # reasonable placeholder for the moment between "pending" and
            # "the model has actually written something".
            artifact_title=(content[:60] or "Document") if document_mode else "",
        )

    # Built in Python rather than the template's own nested-{% if %} string
    # concatenation (what this used to be, before output_mode - a 4th
    # optional param made the "which of these need a leading &" branching
    # genuinely error-prone to extend correctly) - urlencode only ever
    # includes params that are actually set, in a fixed, unambiguous order.
    stream_query_params = {}
    if model_id:
        stream_query_params["model_id"] = model_id
    if research:
        stream_query_params["research"] = "1"
    if agent_persona:
        stream_query_params["agent_persona"] = agent_persona
    if output_mode:
        stream_query_params["output_mode"] = output_mode
    stream_qs = f"?{urlencode(stream_query_params)}" if stream_query_params else ""

    return render(
        request,
        "chat/_message_pending.html",
        {
            "conversation": conversation,
            "pending_message": pending_assistant_message,
            "user_message": user_message,
            "stream_qs": stream_qs,
        },
    )


def _generate_document_bytes(user, prompt):
    """The composer's "File" output mode - unlike image/video (Grok's own
    generation endpoints), a document's content has to come from a normal
    text model first. Deliberately mirrors generate_image/generate_video's
    own shape (just the prompt, no conversation history - a one-shot
    generation, not a conversational reply) rather than routing through
    the full streaming pipeline: same auto-routing as regular chat
    (classify_complexity + select_model_candidates), one synchronous
    provider call, then rendered straight to a .docx via chat/
    document_generation.py's existing render_message_docx - reused as-is
    by handing it a throwaway object with a .content attribute, since
    that function only ever reads that one attribute off whatever it's
    given. Raises MediaGenerationError (the same type generate_image/
    generate_video raise) on any failure, so generate_media's caller
    needs only one except clause regardless of media_mode."""
    from types import SimpleNamespace

    from chat.document_generation import render_message_docx
    from chat.media_generation import MediaGenerationError

    candidates = select_model_candidates(user, classify_complexity(prompt))
    if not candidates:
        raise MediaGenerationError(_("No AI model is enabled and permitted for this user."))

    try:
        content_text = ""
        for chunk in get_provider(candidates[0].provider).stream_chat(
            [{"role": "user", "content": prompt}], candidates[0].model_id, system_prompt=build_system_prompt(user)
        ):
            content_text += chunk.text
            if chunk.done:
                break
    except ProviderError as exc:
        # This message is saved as the assistant's reply, so it must never be
        # the raw provider text (it can echo key fragments, headers or a
        # response body, and names the provider the portal keeps hidden).
        # Same policy as stream_message: log the real error, show a fixed one.
        logger.exception(
            "AI provider call failed during document generation (provider=%s, model=%s)",
            candidates[0].provider,
            candidates[0].model_id,
        )
        capture_exception(exc)
        raise MediaGenerationError(_("The assistant hit a problem generating the document. Please try again.")) from exc

    if not content_text.strip():
        raise MediaGenerationError(_("The model returned an empty response."))
    return render_message_docx(SimpleNamespace(content=content_text))


@login_required
@require_http_methods(["POST"])
def generate_media(request, conversation_id):
    """Grok-only image/video generation (chat/media_generation.py), plus
    the "File" output mode (_generate_document_bytes above) - a genuinely
    different action from post_message above for all three modes, not
    routed through the full streaming pipeline (image/video never were;
    document deliberately mirrors their one-shot shape rather than
    getting its own separate endpoint, since the gating/usage-limit/
    message-creation plumbing below is identical either way). Synchronous:
    the request blocks until generation finishes (a real scaling limit
    for video especially, worth knowing - see media_generation.py's own
    docstring), since there's no streaming/polling story wired up for
    this endpoint's result the way stream_message has for a normal reply."""
    from django.core.files.base import ContentFile

    from chat.media_generation import MediaGenerationError, generate_image, generate_video
    from governance.plans import has_feature

    media_mode = request.POST.get("media_mode", "").strip()
    if media_mode not in ("image", "video", "document"):
        return HttpResponseBadRequest("Unknown media mode")
    # Document generation is gated on the SAME Plan flag as the existing
    # after-the-fact "export this reply as a file" button
    # (export_message_document below) - a different capability from
    # image/video, so it gets its own feature check rather than sharing
    # media_generation's.
    required_feature = "document_generation" if media_mode == "document" else "media_generation"
    if not has_feature(request.user, required_feature):
        message = (
            _("Document generation isn't included in your current plan.")
            if media_mode == "document"
            else _("Image/video generation isn't included in your current plan.")
        )
        return render(request, "chat/_limit_exceeded.html", {"message": message}, status=403)

    prompt = request.POST.get("content", "").strip()
    if not prompt:
        return render(request, "chat/_limit_exceeded.html", {"message": _("Type a prompt first.")}, status=400)

    # No monthly numeric cap for document generation (deliberately, to
    # avoid scope creep - it shares no limit with monthly_document_reads_
    # limit, which counts READING an attachment, a different action from
    # generating one) - only image/video have this check.
    if media_mode in ("image", "video"):
        try:
            check_media_generation_monthly_limit(request.user)
        except UploadRejected as exc:
            return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=403)

    conversation = _owned_conversation_or_404(request, conversation_id)
    try:
        check_usage_limits(request.user, conversation)
    except UsageLimitExceeded as exc:
        return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=429)

    user_message = Message.objects.create(conversation=conversation, role=Message.Role.USER, content=prompt)
    if conversation.title == "New conversation":
        conversation.title = prompt[:60]
        conversation.save(update_fields=["title"])

    try:
        if media_mode == "image":
            file_bytes, filename = generate_image(prompt), "generated-image.png"
        elif media_mode == "video":
            file_bytes, filename = generate_video(prompt), "generated-video.mp4"
        else:
            file_bytes, filename = _generate_document_bytes(request.user, prompt), "generated-document.docx"
    except MediaGenerationError as exc:
        assistant_message = Message.objects.create(
            conversation=conversation, role=Message.Role.ASSISTANT, content=str(exc)
        )
    else:
        assistant_message = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")
        assistant_message.attachment.save(filename, ContentFile(file_bytes, name=filename), save=False)
        assistant_message.attachment_original_name = filename
        assistant_message.attachment_kind = media_mode
        assistant_message.save(update_fields=["attachment", "attachment_original_name", "attachment_kind"])

    return render(
        request,
        "chat/_media_generation_result.html",
        {"conversation": conversation, "user_message": user_message, "assistant_message": assistant_message},
    )


@login_required
@require_http_methods(["POST"])
def post_arena_message(request, conversation_id):
    """Compare mode: one prompt, two models. Creates one user Message and
    TWO pending assistant Messages - each one streams through the exact
    same chat:stream_message view/URL as a normal reply, just with its own
    explicit ?model_id= forcing which model it uses (see
    _pending_assistant_row.html's sse-connect), so no separate streaming
    code path exists for this. Requires "model_selection" (comparing IS
    manually picking two specific models) rather than a dedicated feature
    flag, mirroring post_message's own model_id gate above."""
    from governance.plans import has_feature

    content = request.POST.get("content", "").strip()
    if not content:
        return render(request, "chat/_limit_exceeded.html", {"message": _("Type a message first.")}, status=400)

    from governance.pii import PIIBlocked, apply_pii_rules

    try:
        content = apply_pii_rules(content)
    except PIIBlocked as exc:
        return render(
            request,
            "chat/_limit_exceeded.html",
            {"message": _("This message appears to contain %(kind)s and can't be sent.") % {"kind": exc.kind_label}},
            status=400,
        )

    if not has_feature(request.user, "model_selection"):
        return render(
            request,
            "chat/_limit_exceeded.html",
            {"message": _("Comparing models isn't included in your current plan.")},
            status=403,
        )

    from governance.limits import UsageLimitExceeded
    from governance.plans import check_compare_use_limit, check_message_length_limit

    try:
        check_message_length_limit(request.user, content)
        check_compare_use_limit(request.user)
    except UsageLimitExceeded as exc:
        return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=400)

    visible_ids = set(models_visible_to_user(request.user).values_list("id", flat=True))
    model_a_id = request.POST.get("model_a_id", "").strip()
    model_b_id = request.POST.get("model_b_id", "").strip()
    if (
        not model_a_id
        or not model_b_id
        or model_a_id == model_b_id
        or not model_a_id.isdigit()
        or not model_b_id.isdigit()
        or int(model_a_id) not in visible_ids
        or int(model_b_id) not in visible_ids
    ):
        return render(
            request,
            "chat/_limit_exceeded.html",
            {"message": _("Pick two different models you have access to.")},
            status=400,
        )

    with transaction.atomic():
        conversation = get_object_or_404(
            Conversation.objects.select_for_update(), id=conversation_id, user=request.user
        )
        try:
            check_usage_limits(request.user, conversation)
        except UsageLimitExceeded as exc:
            return render(request, "chat/_limit_exceeded.html", {"message": str(exc)}, status=429)

        Conversation.objects.filter(pk=conversation.pk).update(updated_at=timezone.now())
        user_message = Message.objects.create(conversation=conversation, role=Message.Role.USER, content=content)
        if conversation.title == "New conversation":
            conversation.title = content[:60]
            conversation.save(update_fields=["title"])

        response_a = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")
        response_b = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")
        comparison = ArenaComparison.objects.create(
            conversation=conversation,
            user_message=user_message,
            response_a=response_a,
            response_b=response_b,
            model_a_id=model_a_id,
            model_b_id=model_b_id,
        )

    return render(
        request,
        "chat/_arena_pending.html",
        {
            "conversation": conversation,
            "user_message": user_message,
            "comparison": comparison,
            "stream_qs_a": f"?model_id={model_a_id}" if model_a_id else "",
            "stream_qs_b": f"?model_id={model_b_id}" if model_b_id else "",
        },
    )


@login_required
@require_http_methods(["POST"])
def pick_arena_winner(request, conversation_id, comparison_id):
    conversation = _owned_conversation_or_404(request, conversation_id)
    comparison = get_object_or_404(ArenaComparison, id=comparison_id, conversation=conversation)
    picked_id = request.POST.get("picked_message_id", "").strip()
    if picked_id and int(picked_id) in (comparison.response_a_id, comparison.response_b_id):
        # int(...), not the raw string - comparison.picked_id has to match
        # comparison.response_a_id/response_b_id by type too, since
        # _arena_pair.html compares them directly with `==` (Django
        # templates don't coerce "5" == 5 to True the way Python's `==`
        # against a freshly-queried FK would).
        comparison.picked_id = int(picked_id)
        comparison.save(update_fields=["picked"])
    return render(request, "chat/_arena_pair.html", {"comparison": comparison, "conversation": conversation})


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

        # An edited Live Intelligence question stays grounded: without this
        # the new reply would answer "today's news" from the model's memory.
        # Read BEFORE the delete below wipes the replies that carry it.
        inherited_intel = (
            Message.objects.filter(conversation=conversation, id__gt=message.id, role=Message.Role.ASSISTANT)
            .exclude(live_intel="")
            .values_list("live_intel", flat=True)
            .first()
            or ""
        )
        Message.objects.filter(conversation=conversation, id__gte=message.id).delete()
        Conversation.objects.filter(pk=conversation.pk).update(updated_at=timezone.now())
        new_user_message = Message.objects.create(conversation=conversation, role=Message.Role.USER, content=content)
        pending_assistant_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="",
            live_intel=inherited_intel if live_intelligence.enabled() else "",
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
    message.provider_model_used = None
    message.input_tokens = None
    message.output_tokens = None
    message.estimated_cost = None
    message.save(update_fields=["content", "provider_model_used", "input_tokens", "output_tokens", "estimated_cost"])

    return render(
        request,
        "chat/_pending_assistant_row.html",
        {"conversation": conversation, "pending_message": message, "stream_qs": "?regenerate=1"},
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
                defaults={
                    "user": request.user,
                    "rating": rating,
                    "provider_model_used": message.provider_model_used,
                },
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


_MESSAGE_EXPORT_CONTENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}


@login_required
@require_GET
def export_message_document(request, conversation_id, message_id, doc_format):
    """Turns ONE assistant message into a downloadable file - see
    chat/document_generation.py's own docstring for how this differs from
    export_conversation_pdf/markdown/text above (whole-conversation
    transcript export, no feature gate of its own).

    Gated by governance.plans.has_feature (the per-PLAN subscription
    grant, Plan.feature_flags) - NOT governance.features.require_feature,
    which is the unrelated per-ROLE nav-visibility switch
    (RoleFeatureToggle). Same inline-check style already used for
    "file_upload"/"model_selection" elsewhere in this file, not a
    decorator, since KNOWN_FEATURE_FLAGS checks read the ACTING user's
    Plan, not their role."""
    from governance.plans import has_feature

    if not has_feature(request.user, "document_generation"):
        raise PermissionDenied("Document generation isn't included in your current plan.")
    if doc_format not in _MESSAGE_EXPORT_CONTENT_TYPES:
        return HttpResponseBadRequest("Unknown document format")

    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(Message, id=message_id, conversation=conversation, role=Message.Role.ASSISTANT)

    from chat.document_generation import (
        render_message_docx,
        render_message_pdf,
        render_message_pptx,
        render_message_xlsx,
    )

    renderer = {
        "docx": render_message_docx,
        "xlsx": render_message_xlsx,
        "pptx": render_message_pptx,
        "pdf": render_message_pdf,
    }[doc_format]
    file_bytes = renderer(message)
    response = HttpResponse(file_bytes, content_type=_MESSAGE_EXPORT_CONTENT_TYPES[doc_format])
    response["Content-Disposition"] = f'attachment; filename="message-{message.id}.{doc_format}"'
    return response


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
    with translation.override(user.preferred_language):
        title = _("You're approaching a usage limit")
        body = _("%(label)s: %(pct)s%% used. Contact your administrator if you need more.") % {
            "label": worst["label"],
            "pct": worst["pct"],
        }
    notify(
        user,
        NotificationType.USAGE_WARNING,
        title=title,
        body=body,
        metadata={"metric_label": worst["label"], "metric_pct": worst["pct"]},
    )


def _history_with_attachments(conversation, exclude_message_id):
    """Message history as provider-ready dicts, with the text content of
    any attachment we can extract from (txt/csv/md/json/pdf/docx/xlsx)
    appended inline, delimited via document_extraction.wrap_for_prompt()
    so the model treats it as reference material, never instructions (see
    that module's docstring and the system prompt in chat/prompts.py -
    this is the other required half of the same defense).

    An image attachment instead gets an "images" key - [{"data": base64,
    "mime_type": ...}] - built here regardless of which model will
    ultimately handle it. The vision-capability gate (ProviderModel.
    supports_vision) is applied later in stream_message, per candidate
    model, via _strip_images() - not here, since the history built once
    per request may end up tried against several fallback candidates
    that don't all support vision the same way. chat/providers.py turns
    this generic "images" key into each provider's own wire format."""
    history = []
    messages = conversation.messages.exclude(id=exclude_message_id).order_by("created_at")
    for msg in messages:
        content = msg.content
        images = None
        if msg.attachment:
            name = msg.attachment_original_name
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if extension in IMAGE_EXTENSIONS:
                image = extract_image(msg.attachment, extension)
                if image is not None:
                    images = [image]
                else:
                    content = f"{content}\n\n[Attached image: {name} (couldn't be read)]"
            else:
                extracted = extract_text(msg.attachment, extension) if extension in EXTRACTABLE_EXTENSIONS else None
                if extracted is not None:
                    content = f"{content}\n\n{wrap_for_prompt(name, extracted)}"
                else:
                    content = f"{content}\n\n[Attached file: {name} (not readable by the assistant yet)]"
        turn = {"role": msg.role, "content": content}
        if images:
            turn["images"] = images
        history.append(turn)
    return history


def _strip_images(history):
    """Drops the "images" key before sending history to a candidate model
    whose ProviderModel.supports_vision is False - see
    _history_with_attachments's own docstring for why this gate lives
    here instead of at history-build time."""
    return [{k: v for k, v in turn.items() if k != "images"} for turn in history]


# How long a claimed-but-unfinished generation (Message.is_generating=True)
# is trusted before being treated as abandoned - see stream_message's claim
# logic below. Comfortably longer than any single provider call's own
# timeout*retries (60s timeout, up to 5 SDK-internal retries) so a claim
# is never reclaimed out from under a request that's still legitimately
# running.
STALE_GENERATION_TIMEOUT = timedelta(minutes=10)


@login_required
@require_GET
def stream_message(request, conversation_id, message_id, token):
    """GET-based (SSE requires it) and therefore CSRF-exempt by design -
    the ownership check below alone would still leave a real, predictable
    integer id as the only thing standing between "my own pending
    message" and "a guessed one". `token` (Message.stream_token, a random
    per-message credential set on creation) closes that gap: even
    knowing/guessing a valid conversation_id/message_id pair isn't enough
    without also knowing this."""
    conversation = _owned_conversation_or_404(request, conversation_id)
    message = get_object_or_404(
        Message,
        id=message_id,
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content="",
        stream_token=token,
    )

    # Atomically claim this pending reply before doing any provider work.
    # Without this, two concurrent GETs to this same URL - a duplicate tab,
    # or htmx's sse-connect auto-reconnecting while a previous connection is
    # still technically alive after a network blip - would both pass the
    # content="" filter above and both call the provider independently,
    # doubling the real provider cost for one logical reply. A claim older
    # than STALE_GENERATION_TIMEOUT is treated as abandoned (the process
    # that held it crashed before releasing it) and can be re-claimed rather
    # than wedging the message forever.
    stale_cutoff = timezone.now() - STALE_GENERATION_TIMEOUT
    claimed = (
        Message.objects.filter(pk=message.id, content="")
        .filter(Q(is_generating=False) | Q(generation_started_at__lt=stale_cutoff))
        .update(is_generating=True, generation_started_at=timezone.now())
    )
    if not claimed:

        def already_claimed_stream():
            message.refresh_from_db()
            if message.content:
                # Already finished by the connection that won the race (or
                # by an earlier request entirely) - hand back what's there
                # instead of leaving this connection hanging.
                yield _sse_event("message", message.content)
                yield _sse_event("done", "")
            # else: another connection is generating this same reply right
            # now. Say nothing and let THAT connection's own "done" event
            # resolve it - a manual refresh picks up the finished content
            # if this particular tab never sees it complete.

        response = StreamingHttpResponse(already_claimed_stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    history = _history_with_attachments(conversation, exclude_message_id=message.id)
    requested_model_id = request.GET.get("model_id", "").strip()
    research = request.GET.get("research") == "1"
    # Set by regenerate_message below - "Regenerate" means "give me a
    # fresh attempt", so it must never silently hand back the exact same
    # cached text just because the history hash hasn't changed (the whole
    # point of asking again is that it might not be identical this time).
    is_regenerate = request.GET.get("regenerate") == "1"
    # Composer's "Code" output-mode toggle - post_message already
    # whitelisted this to "" or "code" before it ever reached the stream
    # URL, but re-checked here too rather than trusted, same reasoning as
    # requested_model_id just above.
    output_mode = request.GET.get("output_mode", "").strip()
    if output_mode != "code":
        output_mode = ""
    # Composer's "Generate document" toggle - re-validated here too, same
    # reasoning as output_mode just above (post_message already checked
    # the document_generation feature flag before setting message.
    # is_artifact, but this is reached by a direct GET).
    document_mode = message.is_artifact

    # Re-validated here too (not just trusted from post_message having
    # already checked it) since this is reached by a direct GET, same
    # reasoning as requested_model_id being re-checked against
    # models_visible_to_user just below rather than trusting the caller.
    from chat.prompts import AGENT_PERSONAS
    from governance.plans import has_feature

    agent_persona = request.GET.get("agent_persona", "").strip()
    if agent_persona not in AGENT_PERSONAS or not has_feature(request.user, "agent_mode"):
        agent_persona = ""

    # Mutable holder so the outer finally (below) can see the latest partial
    # text generated so far, from inside the nested generator's per-candidate
    # loop - a plain closed-over local wouldn't be reassignable from there
    # without `nonlocal` scattered through an unrelated loop.
    partial_text = {"text": ""}

    def _generate_reply():
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
                candidates = [ProviderModel.objects.get(id=requested_model_id)]
            else:
                rule_model = match_routing_rule(request.user, conversation)
                if rule_model is not None:
                    candidates = [rule_model]
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

        # Budget automation overrides whatever was just selected above -
        # including an explicit manual pick - once the user's Plan has
        # crossed its spend threshold; it's the softer guardrail that runs
        # before the hard monthly_budget_cap block in
        # governance/limits.py::check_usage_limits (validate_context_tokens
        # below is a different, unrelated cap). Silently falls through to
        # the normal candidates if the fallback model somehow isn't one
        # this user can actually use, rather than blocking the message.
        from governance.plans import get_budget_automation_status

        budget_status = get_budget_automation_status(request.user)
        if (
            budget_status["active"]
            and models_visible_to_user(request.user).filter(id=budget_status["fallback_model"].id).exists()
        ):
            candidates = [budget_status["fallback_model"]]

        # Research mode only actually works on AnthropicProvider (Claude's
        # native web_search server tool - see chat/providers.py) - narrowing
        # candidates to Anthropic-adapter models here means the toggle
        # never silently answers WITHOUT having searched, on whatever
        # non-Claude model auto-routing/a manual pick/budget automation
        # would otherwise have used. Checked here (post_message already
        # confirmed the "research" plan feature) rather than earlier, so
        # it applies after every other candidate-selection rule above,
        # budget automation included.
        if research:
            anthropic_candidates = [c for c in candidates if c.provider.adapter_type == "anthropic"]
            if not anthropic_candidates:
                message.content = "Research mode needs a Claude model enabled for your account."
                message.save(update_fields=["content"])
                yield _sse_event("done", "")
                return
            candidates = anthropic_candidates

        system_prompt = build_system_prompt(
            request.user, agent_persona=agent_persona, output_mode=output_mode, document_mode=document_mode
        )

        # Live Intelligence quick command: retrieve (cache-first) and hand the
        # model ONLY those stories as reference data. If nothing could be
        # retrieved, do NOT call the model at all - asked for "today's news"
        # with no data, it would invent plausible headlines. A fixed reply is
        # saved instead (same "save something, then done" contract as every
        # other exit path here).
        if message.live_intel:
            groups, retrieved_at = live_intelligence.get_stories_for_command(message.live_intel)
            if not groups:
                message.content = live_intelligence.NO_DATA_REPLY
                message.save(update_fields=["content"])
                yield _sse_event("message", live_intelligence.NO_DATA_REPLY)
                yield _sse_event("done", "")
                return
            grounding = live_intelligence.build_grounding_block(groups, retrieved_at)
            system_prompt = system_prompt + "\n\n" + grounding

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
        # Skipped entirely for research mode - a cached answer never
        # actually ran a fresh search, which defeats the whole point of
        # asking for "current information" a second time. Also skipped
        # for an explicit regenerate (see is_regenerate above).
        cached = (
            None
            if (research or is_regenerate)
            else get_cached_response(request.user.id, candidates[0].id, system_prompt, history)
        )
        if cached is not None:
            yield _sse_event("message", cached["text"])
            message.content = cached["text"]
            message.provider_model_used = candidates[0]
            message.input_tokens = cached["input_tokens"]
            message.output_tokens = cached["output_tokens"]
            message.estimated_cost = candidates[0].estimate_cost(
                cached["input_tokens"] or 0, cached["output_tokens"] or 0
            )
            message.served_from_cache = True
            if document_mode and cached["text"]:
                from chat.document_generation import extract_document_title

                message.artifact_title = extract_document_title(cached["text"], fallback=message.artifact_title)
            message.save()
            Conversation.objects.filter(pk=message.conversation_id).update(last_provider_model=candidates[0])
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
            partial_text["text"] = ""
            input_tokens = output_tokens = None
            is_last_candidate = attempt_index == len(candidates) - 1
            history_for_model = history if model_config.supports_vision else _strip_images(history)

            try:
                for chunk in provider.stream_chat(
                    history_for_model, model_config.model_id, system_prompt=system_prompt, enable_web_search=research
                ):
                    if chunk.text:
                        full_text += chunk.text
                        partial_text["text"] = full_text
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
                    model_config.model_id,
                )
                # Tagged (not just logged) so Sentry can filter/group by
                # provider and model - every ProviderError shares the same
                # stack, so without these an Anthropic overload and an
                # OpenAI auth failure land in one undifferentiated issue.
                # Slugs/ids only: never the prompt, reply, or the upstream
                # exception text as a tag. request_id is already a scope tag
                # (accounts.middleware.RequestIDMiddleware).
                with new_scope() as scope:
                    scope.set_tag("ai.provider", model_config.provider.slug)
                    scope.set_tag("ai.model", model_config.model_id)
                    capture_exception(exc)
                message.content = full_text or "The assistant hit a problem generating a response. Please try again."
                message.provider_model_used = model_config
                message.save(update_fields=["content", "provider_model_used"])
                yield _sse_event("done", "")
                return

            message.content = full_text
            message.provider_model_used = model_config
            message.input_tokens = input_tokens
            message.output_tokens = output_tokens
            message.estimated_cost = model_config.estimate_cost(input_tokens or 0, output_tokens or 0)
            if document_mode and full_text:
                from chat.document_generation import extract_document_title

                message.artifact_title = extract_document_title(full_text, fallback=message.artifact_title)
            message.save()
            Conversation.objects.filter(pk=message.conversation_id).update(last_provider_model=model_config)
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

    def event_stream():
        try:
            yield from _generate_reply()
        finally:
            # Always release the claim. If content is still "" here, none
            # of _generate_reply's own save points got the chance to run -
            # most commonly because the client disconnected mid-stream
            # (Django/WSGI closes the generator, raising GeneratorExit at
            # whichever yield was in flight; that propagates through
            # `yield from` and still lands here, since Python always runs a
            # pending finally on generator close). Persist whatever partial
            # text had been generated so far instead of leaving the row
            # wedged at content="" with is_generating stuck True - a reload
            # then shows the partial answer as-is rather than hanging or
            # silently re-running (and re-billing) the whole request.
            Message.objects.filter(pk=message.id, content="", is_generating=True).update(
                content=partial_text["text"], is_generating=False
            )
            Message.objects.filter(pk=message.id, is_generating=True).update(is_generating=False)

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
