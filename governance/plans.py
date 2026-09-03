"""Plan resolution and precedence rules.

MIGRATION APPROACH (documented per the spec's explicit request): per-user
`UsageLimit` and `UserModelPermission` rows are kept as explicit overrides
layered ON TOP of a user's Plan, not replaced by it. Reasoning: those tables
are already deeply wired into governance/limits.py, chat/router.py, the
admin Limits/Model-permissions screens, and ~90 existing tests. Fully
migrating away would be a much larger, riskier rewrite of already-working
enforcement code for a single feature pass. Precedence (most specific wins):

  Limits:        personal UsageLimit > department UsageLimit > user's Plan > system default
  Model access:  ((Plan.allowed_models MINUS Team.disabled_models) UNION explicit is_allowed=True)
                 MINUS explicit is_allowed=False
  Features:      Plan.feature_flags only (no per-user override exists for these)

A Manager's per-team model restriction (Team.disabled_models) sits between
the Plan and personal overrides: it can only narrow what the Plan already
grants, never widen it, and an explicit personal is_allowed=True override
still wins over it (that's an Admin's more-specific call for one person).

A user with no Plan assignment at all is treated as unrestricted (matches
this app's pre-Plan behavior) rather than silently locked out - Plans only
ever *restrict* on top of that baseline once assigned.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

GRACE_PERIOD_DAYS = getattr(settings, "DEMO_GRACE_PERIOD_DAYS", 2)


def get_assignment(user):
    from governance.models import UserPlanAssignment

    return UserPlanAssignment.objects.select_related("plan", "previous_plan").filter(user=user).first()


def assign_plan(user, plan, assigned_by=None):
    """The only correct way to change a user's plan - computes expiry for
    demo plans, tracks previous_plan, and returns the assignment. Callers
    are responsible for their own audit_log entry (the assigner knows the
    "why", e.g. "admin change" vs "self-service approval" vs "auto-expiry")."""
    from governance.models import UserPlanAssignment

    expires_at = None
    if plan.is_demo and plan.demo_duration_days:
        expires_at = timezone.now() + timedelta(days=plan.demo_duration_days)

    assignment, _ = UserPlanAssignment.objects.get_or_create(user=user, defaults={"plan": plan})
    assignment.previous_plan = assignment.plan
    assignment.plan = plan
    assignment.expires_at = expires_at
    assignment.assigned_by = assigned_by
    assignment.assigned_at = timezone.now()
    assignment.save()
    return assignment


def assign_default_plan_if_missing(user):
    """Called on user creation (see accounts/signals.py). No-op if the user
    already has an assignment, or if no default plan is configured yet."""
    from governance.models import Plan, UserPlanAssignment

    if UserPlanAssignment.objects.filter(user=user).exists():
        return None
    default_plan = Plan.objects.filter(is_default=True, is_active=True).first()
    if default_plan is None:
        return None
    return assign_plan(user, default_plan, assigned_by=None)


def get_plan_status(user):
    """The single source of truth for "where does this user's plan stand
    right now". Returns a dict:
      plan, assignment: the Plan/UserPlanAssignment objects (or None)
      state: "none" | "active" | "grace" | "expired"
      days_remaining: whole days left before expiry (active demo plans only)
      grace_days_remaining: whole days left in the post-expiry grace window
    Purely a read - never mutates anything, so it's safe to call on every
    request (no Celery/cron dependency for the actual blocking behavior;
    see the module docstring in governance/tasks.py notes for why)."""
    assignment = get_assignment(user)
    if assignment is None:
        return {"plan": None, "assignment": None, "state": "none", "days_remaining": None, "grace_days_remaining": None}

    plan = assignment.plan
    if not plan.is_demo or assignment.expires_at is None:
        return {
            "plan": plan,
            "assignment": assignment,
            "state": "active",
            "days_remaining": None,
            "grace_days_remaining": None,
        }

    now = timezone.now()
    if now <= assignment.expires_at:
        days_remaining = max(0, (assignment.expires_at - now).days)
        return {
            "plan": plan,
            "assignment": assignment,
            "state": "active",
            "days_remaining": days_remaining,
            "grace_days_remaining": None,
        }

    grace_end = assignment.expires_at + timedelta(days=GRACE_PERIOD_DAYS)
    if now <= grace_end:
        grace_days_remaining = max(0, (grace_end - now).days)
        return {
            "plan": plan,
            "assignment": assignment,
            "state": "grace",
            "days_remaining": 0,
            "grace_days_remaining": grace_days_remaining,
        }

    return {"plan": plan, "assignment": assignment, "state": "expired", "days_remaining": 0, "grace_days_remaining": 0}


def effective_allowed_model_ids(user):
    """Plan grant set, adjusted by explicit per-user overrides. Returns
    None to mean "unrestricted" (no plan assigned at all)."""
    from chat.models import UserModelPermission

    status = get_plan_status(user)
    plan = status["plan"]

    denied = set(
        UserModelPermission.objects.filter(user=user, is_allowed=False).values_list("model_config_id", flat=True)
    )
    granted_extra = set(
        UserModelPermission.objects.filter(user=user, is_allowed=True).values_list("model_config_id", flat=True)
    )

    if plan is None:
        return None  # caller should treat this as "don't filter"

    base_ids = set(plan.allowed_models.values_list("id", flat=True))
    if user.team_id:
        base_ids -= set(user.team.disabled_models.values_list("id", flat=True))
    return (base_ids | granted_extra) - denied


def _model_config_ids_to_provider_model_ids(mc_ids):
    """(provider slug, model name) match - the translation every ModelConfig
    -> ProviderModel override/restriction goes through in
    effective_allowed_provider_model_ids below, factored out since three
    separate ModelConfig-keyed sources (UserModelPermission x2, Team.
    disabled_models) all need the same translation. An id with no
    ProviderModel counterpart (shouldn't happen once step 1's migration
    has run, but not assumed) is simply dropped - the safe direction to
    fail in for both a grant and a restriction: a dropped grant denies
    nothing extra, a dropped restriction never widens access either,
    since the model wasn't going to be in the ProviderModel-side base set
    unless a Plan/Provider already includes it independently."""
    from chat.models import ModelConfig
    from providers.models import ProviderModel

    if not mc_ids:
        return set()
    pairs = ModelConfig.objects.filter(id__in=mc_ids).values_list("provider", "model_name")
    result = set()
    for provider_slug, model_name in pairs:
        pm_id = (
            ProviderModel.objects.filter(provider__slug=provider_slug, model_id=model_name)
            .values_list("id", flat=True)
            .first()
        )
        if pm_id is not None:
            result.add(pm_id)
    return result


def effective_allowed_provider_model_ids(user):
    """Same precedence/meaning as effective_allowed_model_ids, but returns
    providers.ProviderModel ids - the routing-facing counterpart
    chat/router.py uses now that it's off chat.models.ModelConfig (step 3
    of the ModelConfig -> ProviderModel migration; see chat/providers.py
    and providers/management/commands/migrate_models_to_provider_model.py
    for steps 1-2).

    Neither UserModelPermission nor Team.disabled_models were migrated
    (narrower, admin-UI-only scope than Plan.allowed_models/Message.
    model_used - deliberately out of scope for this step) - both still
    target ModelConfig, so each is translated to its ProviderModel
    equivalent via _model_config_ids_to_provider_model_ids rather than
    requiring a schema change to either."""
    from chat.models import UserModelPermission

    status = get_plan_status(user)
    plan = status["plan"]

    denied_mc_ids = set(
        UserModelPermission.objects.filter(user=user, is_allowed=False).values_list("model_config_id", flat=True)
    )
    granted_mc_ids = set(
        UserModelPermission.objects.filter(user=user, is_allowed=True).values_list("model_config_id", flat=True)
    )
    denied = _model_config_ids_to_provider_model_ids(denied_mc_ids)
    granted_extra = _model_config_ids_to_provider_model_ids(granted_mc_ids)

    if plan is None:
        return None  # caller should treat this as "don't filter"

    base_ids = set(plan.allowed_provider_models.values_list("id", flat=True))
    if user.team_id:
        team_disabled_mc_ids = set(user.team.disabled_models.values_list("id", flat=True))
        base_ids -= _model_config_ids_to_provider_model_ids(team_disabled_mc_ids)
    return (base_ids | granted_extra) - denied


class _PlanLimitFallback:
    """A minimal UsageLimit-shaped read-only stand-in so _effective_limit()
    can treat "fall back to the user's Plan" the same as any other limit
    source, without UsageLimit needing to know about Plan at all."""

    def __init__(self, plan):
        self.daily_token_cap = plan.daily_token_limit
        self.monthly_token_cap = plan.monthly_token_limit
        self.session_limit = plan.messages_per_session_limit
        self.budget_cap_currency = plan.monthly_budget_cap
        self.max_upload_size_mb = None
        self.allowed_file_extensions = ""


def plan_limit_fallback(user):
    status = get_plan_status(user)
    plan = status["plan"]
    if plan is None:
        return None
    return _PlanLimitFallback(plan)


def get_user_overrides(user):
    """The personal UsageLimit row and UserModelPermission rows that sit on
    top of this user's Plan (see precedence rules above) - the raw material
    for the admin "N custom overrides beyond Plan defaults" indicator."""
    from chat.models import UserModelPermission
    from governance.models import UsageLimit

    usage_limit = UsageLimit.objects.filter(user=user).first()
    model_permissions = list(
        UserModelPermission.objects.filter(user=user)
        .select_related("model_config")
        .order_by("model_config__display_name")
    )
    return {"usage_limit": usage_limit, "model_permissions": model_permissions}


def count_user_overrides(user):
    overrides = get_user_overrides(user)
    return (1 if overrides["usage_limit"] else 0) + len(overrides["model_permissions"])


def clear_user_overrides(user):
    """Deletes the personal UsageLimit row and all UserModelPermission rows
    for this user, so their Plan alone governs again. Returns a short
    description of what was removed, for the audit log entry."""
    from chat.models import UserModelPermission

    overrides = get_user_overrides(user)
    parts = []
    if overrides["usage_limit"]:
        overrides["usage_limit"].delete()
        parts.append("personal usage limit")
    permission_count = len(overrides["model_permissions"])
    if permission_count:
        UserModelPermission.objects.filter(user=user).delete()
        parts.append(f"{permission_count} model permission override(s)")
    return ", ".join(parts) or "nothing to clear"


def has_feature(user, flag_name):
    """True when no plan is assigned (unrestricted baseline) or the flag
    is explicitly on for the user's plan."""
    status = get_plan_status(user)
    plan = status["plan"]
    if plan is None:
        return True
    return plan.has_feature(flag_name)


def check_session_creation_limit(user):
    """Raise UsageLimitExceeded if the user has already started as many
    conversations today as their plan allows. Separate from
    check_usage_limits() in governance/limits.py because this caps *new
    conversations*, not messages within one - a Plan-only concept, so it
    lives here rather than being bolted onto the UsageLimit-based checks."""
    from governance.limits import UsageLimitExceeded

    status = get_plan_status(user)
    plan = status["plan"]
    if plan is None or plan.sessions_per_day_limit is None:
        return

    if status["state"] == "expired":
        raise UsageLimitExceeded("Your trial has ended — contact your administrator.")

    from chat.models import Conversation

    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    started_today = Conversation.objects.filter(user=user, created_at__gte=today_start).count()
    if started_today >= plan.sessions_per_day_limit:
        raise UsageLimitExceeded(
            f"You've reached your plan's limit of {plan.sessions_per_day_limit} new conversation(s) per day."
        )


def _request_count_window(user, plan, conversation):
    """(sent_count, window_label) for plan.max_requests_per_period's window -
    shared by the real enforcement check below and the read-only status
    snapshot, so the two can never drift on what counts as "sent". Returns
    (None, None) when this plan has no request-count cap configured, or
    when period="session" but no conversation was given to count within."""
    from chat.models import Message

    if plan is None or plan.max_requests_per_period is None or not plan.period:
        return None, None

    if plan.period == "session":
        if conversation is None:
            return None, None
        sent = conversation.messages.filter(role=Message.Role.USER).count()
        window_label = _("this conversation")
    elif plan.period == "day":
        start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        sent = Message.objects.filter(conversation__user=user, role=Message.Role.USER, created_at__gte=start).count()
        window_label = _("today")
    elif plan.period == "month":
        start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        sent = Message.objects.filter(conversation__user=user, role=Message.Role.USER, created_at__gte=start).count()
        window_label = _("this month")
    else:
        return None, None
    return sent, window_label


def check_request_count_limit(user, conversation):
    """Raise UsageLimitExceeded if sending another message would exceed
    this user's Plan's max_requests_per_period, counted over whatever
    window `period` specifies. Deliberately independent of and additional
    to the token-VOLUME caps in governance/limits.py::check_usage_limits()
    - request count and token volume are different things worth capping
    separately (many short messages vs. a few very long ones can each trip
    one cap without tripping the other)."""
    from governance.limits import UsageLimitExceeded

    plan = get_plan_status(user)["plan"]
    sent, window_label = _request_count_window(user, plan, conversation)
    if sent is None:
        return

    if sent >= plan.max_requests_per_period:
        period_label = plan.get_period_display().lower()
        raise UsageLimitExceeded(
            f"You've reached your plan's limit of {plan.max_requests_per_period} message(s) per {period_label} "
            f"({window_label}). Try again later or contact your administrator."
        )


def get_request_count_status(user, conversation=None):
    """Read-only {used, cap, window_label} snapshot of this user's request-
    count usage against their Plan's max_requests_per_period, or None if
    their plan has no such cap configured. Mirrors check_request_count_limit's
    window logic without enforcing anything - for the chat header's usage
    pill, which needs to show a live count without gating anything."""
    plan = get_plan_status(user)["plan"]
    sent, window_label = _request_count_window(user, plan, conversation)
    if sent is None:
        return None
    return {"used": sent, "cap": plan.max_requests_per_period, "window_label": window_label}


# ~4 characters per token is the standard rough estimate for English text
# (no tokenizer dependency added just for this - it's an approximation, not
# an exact count, and is documented as such rather than presented as exact).
_CHARS_PER_TOKEN_ESTIMATE = 4


def validate_context_tokens(user, system_prompt, history):
    """Raise UsageLimitExceeded if the assembled prompt (system prompt +
    full message history, including any extracted-attachment text) for the
    NEXT request would exceed this user's Plan's max_context_tokens.
    Rejects rather than truncates: silently dropping earlier turns to fit
    would send the model a coherence-broken conversation and could produce
    a confusing reply with no indication anything was cut - an explicit,
    visible rejection (matching how every other Plan limit in this app
    behaves) is safer than a silent degradation the user has no way to
    notice from the reply alone."""
    from governance.limits import UsageLimitExceeded

    status = get_plan_status(user)
    plan = status["plan"]
    if plan is None or plan.max_context_tokens is None:
        return

    total_chars = len(system_prompt) + sum(len(turn.get("content", "")) for turn in history)
    estimated = max(1, total_chars // _CHARS_PER_TOKEN_ESTIMATE)
    if estimated > plan.max_context_tokens:
        raise UsageLimitExceeded(
            f"This conversation is too long for your plan's per-request context limit "
            f"(~{estimated} tokens estimated, {plan.max_context_tokens} allowed). "
            "Start a new chat or contact your administrator for a higher limit."
        )


def engagement_score(user):
    """Simple heuristic for surfacing "engaged demo users worth upgrading":
    (% of their plan's monthly token limit used) / (% of their trial
    elapsed). >1 means burning through their trial faster than time is
    passing - a genuine usage signal, not a scored/ML system. Returns None
    when it doesn't apply (not on a demo plan, or no token limit set)."""
    from django.db.models import F, Sum

    from chat.models import Message

    status = get_plan_status(user)
    plan, assignment = status["plan"], status["assignment"]
    if plan is None or not plan.is_demo or not plan.monthly_token_limit or assignment.expires_at is None:
        return None

    total_duration = plan.demo_duration_days or 1
    elapsed = (timezone.now() - assignment.assigned_at).total_seconds() / 86400
    pct_elapsed = max(elapsed / total_duration, 0.01)

    used = (
        Message.objects.filter(conversation__user=user, role=Message.Role.ASSISTANT).aggregate(
            total=Sum(F("input_tokens") + F("output_tokens")),
        )["total"]
        or 0
    )
    pct_used = used / plan.monthly_token_limit

    return round(pct_used / pct_elapsed, 2)
