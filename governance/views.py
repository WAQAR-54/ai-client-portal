import math
from decimal import Decimal, InvalidOperation

from django.contrib import messages as django_messages
from django.contrib.auth.forms import SetPasswordForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_http_methods
from django.views.generic import ListView, TemplateView

from accounts.models import Department, Team, User
from accounts.permissions import AdminRequiredMixin, ManagerRequiredMixin, SuperAdminRequiredMixin, role_required
from chat.models import Conversation, Message, MessageFeedback, ModelConfig, PromptTemplate, UserModelPermission
from governance.audit import log_action
from governance.features import RequireFeatureMixin, require_feature
from governance.limits import _effective_limit, _metric
from governance.models import (
    ADMIN_NAV_FEATURES,
    AuditLog,
    KNOWN_FEATURE_FLAGS,
    Plan,
    ROLE_FEATURE_ROLES,
    RoleFeatureToggle,
    SystemPromptVersion,
    UpgradeRequest,
    USER_CHAT_FEATURES,
    UsageLimit,
)
from governance.plans import (
    assign_plan,
    clear_user_overrides,
    count_user_overrides,
    engagement_score,
    get_assignment,
    get_plan_status,
    get_user_overrides,
)
from providers.models import ProviderModel

# ---------- Department-scoping helpers ----------
# A plain Admin only ever sees/touches their own department; a SuperAdmin is
# unscoped. Every governance queryset/lookup below routes through one of
# these rather than re-deriving the same `if role == ADMIN: filter(...)`
# check ad hoc, so the scoping rule lives in one place per data shape.


def _is_scoped_admin(user):
    """True only for a plain (non-super) Admin — the one role that's
    actually department-restricted. SuperAdmin is unscoped; Manager/User
    never reach these governance views at all (blocked by the role
    mixins/decorators before this is ever checked)."""
    return user.role == User.Role.ADMIN


def _scope_users(request, qs):
    if _is_scoped_admin(request.user):
        qs = qs.filter(department_id=request.user.department_id)
    return qs


def _scope_by_user_department(request, qs, path="user__department_id"):
    """For querysets one hop away from User via `path` (e.g. Message via
    conversation__user__department_id, UpgradeRequest via
    user__department_id)."""
    if _is_scoped_admin(request.user):
        qs = qs.filter(**{path: request.user.department_id})
    return qs


def _assignable_roles_and_scope(request):
    """(assignable_roles, teams_qs, departments_qs) for the Users edit UI -
    shared by UserListView (the whole table) and user_edit_form (one user's
    edit modal) so the two can never drift on what a scoped Admin is
    allowed to see/assign. A scoped Admin can only ever promote/demote
    between User and Manager, within their own department — never create
    another Admin or a SuperAdmin (that would let an Admin escalate their
    own department's access beyond what was granted to them). A SuperAdmin
    can set anyone to any role."""
    if _is_scoped_admin(request.user):
        assignable_roles = [(v, l) for v, l in User.Role.choices if v in (User.Role.USER, User.Role.MANAGER)]
        teams_qs = Team.objects.filter(department_id=request.user.department_id)
        departments_qs = Department.objects.filter(id=request.user.department_id)
    else:
        assignable_roles = list(User.Role.choices)
        teams_qs = Team.objects.select_related("department").order_by("department__name", "name")
        departments_qs = Department.objects.order_by("name")
    return assignable_roles, teams_qs, departments_qs


def _get_scoped_user_or_403(request, user_id):
    """Fetch a target user for a write action, enforcing that a scoped
    Admin can only ever touch users in their own department — raises
    PermissionDenied (403), not a silent 404, so this is testable as an
    explicit access-control decision rather than looking like "not found"."""
    target = get_object_or_404(User, id=user_id)
    if _is_scoped_admin(request.user) and target.department_id != request.user.department_id:
        raise PermissionDenied("That user is outside your department.")
    return target


def _scope_teams(request, qs):
    if _is_scoped_admin(request.user):
        qs = qs.filter(department_id=request.user.department_id)
    return qs


def _get_team_member_or_403(request, user_id):
    """Fetch a target user for a Manager write action, enforcing that a
    Manager can only ever touch members of their OWN team — raises
    PermissionDenied (403), not a silent 404, same reasoning as
    _get_scoped_user_or_403 above for a department-scoped Admin. Returns
    (target, team)."""
    team = getattr(request.user, "managed_team", None)
    if team is None:
        raise PermissionDenied("You don't manage a team.")
    target = get_object_or_404(User, id=user_id)
    if target.team_id != team.id:
        raise PermissionDenied("That user isn't on your team.")
    return target, team


def _toggle_user_model_permission(actor, target, model_config):
    """The SuperAdmin's org-wide ModelPermissionsView (legacy Models page,
    "who can use") - toggles between explicit-deny and explicit-allow,
    starting from "allowed" (every org-enabled ModelConfig is available via
    Plan by default, so the first click denies it for this one person)."""
    permission, _ = UserModelPermission.objects.get_or_create(
        user=target, model_config=model_config, defaults={"is_allowed": True}
    )
    old_value = permission.is_allowed
    permission.is_allowed = not permission.is_allowed
    permission.save(update_fields=["is_allowed"])
    log_action(actor, "model.permission_change", permission, old_value=old_value, new_value=permission.is_allowed)
    return permission


def _toggle_manager_assigned_model(actor, target, provider_model):
    """A Manager's per-team-member assignment from the admin-curated
    is_manager_assignable pool (governance:toggle_member_model_permission)
    - the opposite starting point from _toggle_user_model_permission above:
    there's no implicit org-wide access to toggle away from here (a model
    only ever ends up in this pool because an admin opted it in, separately
    from whether it's in anyone's Plan), so this is a pure additive
    grant/ungrant, not a deny/allow flip. The row's mere existence (always
    is_allowed=True) means "granted"; toggling off deletes it outright
    rather than leaving a meaningless explicit deny behind.

    This grant is deliberately allowed to win over the Manager's own
    Team.disabled_models restriction for this one person - see
    governance/plans.py's module docstring: "an explicit personal
    is_allowed=True override still wins over it (that's an Admin's [/here,
    a Manager's] more-specific call for one person)"."""
    existing = UserModelPermission.objects.filter(user=target, provider_model=provider_model).first()
    if existing:
        existing.delete()
        log_action(actor, "model.manager_assignment_removed", provider_model, old_value=target.email)
        return None
    permission = UserModelPermission.objects.create(user=target, provider_model=provider_model, is_allowed=True)
    log_action(actor, "model.manager_assignment_granted", permission, new_value=target.email)
    return permission


def _scope_limits(request, qs):
    """A UsageLimit targets either a user or a department (never both, per
    its own CheckConstraint) — scope on whichever is set."""
    if _is_scoped_admin(request.user):
        dept_id = request.user.department_id
        qs = qs.filter(Q(user__department_id=dept_id) | Q(department_id=dept_id))
    return qs


def _scope_audit_logs(request, qs):
    """AuditLog's target is a loose (target_type, target_id) pair, not a
    real FK, so it can't be scoped with a simple join like the other
    querysets here. An entry counts as "in the Admin's department" if
    either the actor belongs to it, or the entry is about a User (role/
    plan/department change, suspension, ...) who belongs to it."""
    if not _is_scoped_admin(request.user):
        return qs
    dept_id = request.user.department_id
    dept_user_ids = [str(uid) for uid in User.objects.filter(department_id=dept_id).values_list("id", flat=True)]
    return qs.filter(Q(actor__department_id=dept_id) | Q(target_type="User", target_id__in=dept_user_ids))


def _querystring_without(request, *exclude_keys):
    """Current GET querystring with the given keys stripped — used to build
    pagination links that preserve whatever search/filter is active."""
    qd = request.GET.copy()
    for key in exclude_keys:
        qd.pop(key, None)
    return qd.urlencode()


class FilterableListMixin:
    """Shared plumbing for admin list screens with a live search/filter
    toolbar: swap to a partial-only template for htmx requests (so the
    toolbar's live search only ever re-renders the list, not the whole
    page), while a full page load/refresh/bookmark with the same query
    params renders identically via the full template that includes the
    same partial."""

    partial_template_name = None

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return [self.partial_template_name]
        return super().get_template_names()


def _org_usage_overview(users=None):
    """Aggregate, read-only snapshot for the admin Overview page's usage
    ring: average pct-of-monthly-token-cap across users who have an
    effective limit with a monthly_token_cap set. Purely additive — reuses
    _effective_limit()/_metric() from governance.limits but never touches
    check_usage_limits()/validate_upload(). `users` defaults to everyone
    (SuperAdmin); DashboardView passes a department-scoped queryset for a
    plain Admin."""
    if users is None:
        users = User.objects.all()
    month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    pct_values = []
    over_80_count = 0
    for user in users:
        limit = _effective_limit(user)
        if limit is None or limit.monthly_token_cap is None:
            continue
        used = Message.objects.filter(
            conversation__user=user,
            role=Message.Role.ASSISTANT,
            created_at__gte=month_start,
        ).aggregate(total=Sum(F("input_tokens") + F("output_tokens")))["total"]
        metric = _metric("", used, limit.monthly_token_cap)
        pct_values.append(metric["pct"])
        if metric["pct"] >= 80:
            over_80_count += 1

    if not pct_values:
        return {"has_data": False}
    avg_pct = round(sum(pct_values) / len(pct_values))
    level = "danger" if avg_pct >= 100 else "warn" if avg_pct >= 80 else "ok"
    return {
        "has_data": True,
        "avg_pct": avg_pct,
        "level": level,
        "tracked_users": len(pct_values),
        "over_80_count": over_80_count,
    }


@role_required(User.Role.ADMIN)
@require_GET
def global_search(request):
    """Backs the admin topbar's search box - a handful of matches each
    from Users/Plans/Audit logs, scoped the same way the rest of the admin
    console is for a department-scoped Admin. Not a full-text index, just
    icontains across a few fields - fine at this data volume."""
    query = request.GET.get("q", "").strip()
    if len(query) < 2:
        return render(request, "governance/_global_search_results.html", {"query": query})

    users = _scope_users(request, User.objects.filter(email__icontains=query)).order_by("email")[:5]
    plans = Plan.objects.filter(name__icontains=query).order_by("name")[:5]
    logs = (
        _scope_audit_logs(request, AuditLog.objects.select_related("actor"))
        .filter(Q(action_type__icontains=query) | Q(actor__email__icontains=query) | Q(target_type__icontains=query))
        .order_by("-timestamp")[:5]
    )

    return render(
        request,
        "governance/_global_search_results.html",
        {"query": query, "results_users": users, "results_plans": plans, "results_logs": logs},
    )


class DashboardView(AdminRequiredMixin, TemplateView):
    template_name = "governance/dashboard.html"

    def get_context_data(self, **kwargs):
        local_hour = timezone.localtime().hour
        if local_hour < 12:
            greeting_time = _("morning")
        elif local_hour < 18:
            greeting_time = _("afternoon")
        else:
            greeting_time = _("evening")
        # No first/last name field on User - the email's local part is the
        # closest thing to a display name this app has.
        greeting_name = self.request.user.email.split("@")[0].replace(".", " ").title()

        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        assistant_messages = Message.objects.filter(role=Message.Role.ASSISTANT)
        assistant_messages = _scope_by_user_department(
            self.request, assistant_messages, "conversation__user__department_id"
        )
        scoped_users = _scope_users(self.request, User.objects.all())
        month_messages = assistant_messages.filter(created_at__gte=month_start)

        today = timezone.localdate()
        fourteen_days_ago = today - timezone.timedelta(days=13)
        daily_rows = (
            assistant_messages.filter(created_at__date__gte=fourteen_days_ago)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(tokens=Sum(F("input_tokens") + F("output_tokens")), cost=Sum("estimated_cost"))
        )
        by_day = {row["day"]: row for row in daily_rows}
        # Zero-fill every day in the 14-day window, not just days that
        # happen to have a message row — otherwise sparse data collapses
        # the x-axis down to a single point instead of a real 14-day range.
        date_range = [fourteen_days_ago + timezone.timedelta(days=i) for i in range(14)]
        daily_labels = [d.strftime("%b %d") for d in date_range]
        daily_tokens = [by_day[d]["tokens"] or 0 if d in by_day else 0 for d in date_range]
        daily_cost = [float(by_day[d]["cost"] or 0) if d in by_day else 0.0 for d in date_range]

        # Merged across both the legacy ModelConfig-based field (pre-step-3-
        # cutover messages) and the current ProviderModel-based one (every
        # message since) - see chat/providers.py's module docstring for the
        # cutover. Two separate aggregates merged in Python since they join
        # through different FKs; without merging, this chart would show
        # shrinking model coverage over time as legacy rows age out.
        legacy_by_model = (
            assistant_messages.exclude(model_used__isnull=True)
            .values("model_used__model_name")
            .annotate(cost=Sum("estimated_cost"))
        )
        current_by_model = (
            assistant_messages.exclude(provider_model_used__isnull=True)
            .values("provider_model_used__model_id")
            .annotate(cost=Sum("estimated_cost"))
        )
        cost_by_label = {}
        for row in legacy_by_model:
            cost_by_label[row["model_used__model_name"]] = cost_by_label.get(row["model_used__model_name"], 0) + float(
                row["cost"] or 0
            )
        for row in current_by_model:
            cost_by_label[row["provider_model_used__model_id"]] = cost_by_label.get(
                row["provider_model_used__model_id"], 0
            ) + float(row["cost"] or 0)
        top_models = sorted(cost_by_label.items(), key=lambda item: item[1], reverse=True)[:8]
        model_labels = [label for label, _cost in top_models]
        model_cost = [cost for _label, cost in top_models]

        role_counts = scoped_users.values("role").annotate(count=Count("id")).order_by("-count")
        role_labels = [dict(User.Role.choices).get(row["role"], row["role"]) for row in role_counts]
        role_values = [row["count"] for row in role_counts]

        scoped_conversations = Conversation.objects.all()
        scoped_conversations = _scope_by_user_department(self.request, scoped_conversations, "user__department_id")

        pending_requests_qs = _scope_by_user_department(
            self.request,
            UpgradeRequest.objects.select_related("user", "current_plan", "requested_plan").filter(
                status=UpgradeRequest.Status.PENDING
            ),
        )
        recent_upgrade_requests = pending_requests_qs.order_by("-created_at")[:5]

        # Sparklines reuse the 14-day series already computed above rather
        # than running a second aggregation query - last 7 days, normalized
        # to a 0-100 bar height so the template can render plain divs.
        def _spark(values):
            last_7 = values[-7:]
            peak = max(last_7) or 1
            return [round(v / peak * 100) for v in last_7]

        # Compact display for large counters ("2.4" + "M") - the KPI count-up
        # animation reads data-target/data-suffix back apart, so these are
        # returned as a (number-string, suffix) pair rather than one string.
        def _compact(value):
            value = float(value)
            if value >= 1_000_000:
                num = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
                return num, "M"
            if value >= 1_000:
                num = f"{value / 1_000:.1f}".rstrip("0").rstrip(".")
                return num, "K"
            return f"{int(value):,}", ""

        # Horizontal bar-list rows (admin dashboard "Cost by model" card) -
        # width % is computed here, once, rather than re-derived client-side.
        def _bar_rows(labels, values):
            peak = max(values) if values else 0
            peak = peak or 1
            return [
                {"label": label, "value": value, "pct": round(value / peak * 100)}
                for label, value in zip(labels, values)
            ]

        # Donut segments (admin dashboard "Users by role" card) - stroke-dasharray/
        # -dashoffset computed server-side against a fixed r=15.5 circle (matching
        # the SVG markup in the template) so the template just plugs in numbers.
        def _donut_segments(labels, values):
            total = sum(values) or 1
            circumference = round(2 * math.pi * 15.5, 1)
            colors = [
                "var(--secondary)",
                "var(--accent)",
                "var(--color-surface-active)",
                "var(--warn)",
                "var(--color-danger)",
            ]
            segments = []
            offset = 0.0
            for i, (label, value) in enumerate(zip(labels, values)):
                seg_len = value / total * circumference
                segments.append(
                    {
                        "label": label,
                        "pct": round(value / total * 100),
                        "color": colors[i % len(colors)],
                        "dasharray": f"{seg_len:.2f} {circumference - seg_len:.2f}",
                        "dashoffset": round(-offset, 2),
                    }
                )
                offset += seg_len
            return segments

        tokens_all_time = assistant_messages.aggregate(total=Sum(F("input_tokens") + F("output_tokens")))["total"] or 0
        tokens_all_time_target, tokens_all_time_suffix = _compact(tokens_all_time)
        month_tokens_total = month_messages.aggregate(total=Sum(F("input_tokens") + F("output_tokens")))["total"] or 0
        month_tokens_target, month_tokens_suffix = _compact(month_tokens_total)
        cost_all_time = assistant_messages.aggregate(total=Sum("estimated_cost"))["total"] or 0
        month_cost_total = month_messages.aggregate(total=Sum("estimated_cost"))["total"] or 0
        users_count = scoped_users.count()
        conversations_count = scoped_conversations.count()

        return super().get_context_data(**kwargs) | {
            "total_users": users_count,
            "total_conversations": conversations_count,
            "total_users_display": f"{users_count:,}",
            "total_conversations_display": f"{conversations_count:,}",
            "total_tokens_all_time": tokens_all_time,
            "total_cost_all_time": cost_all_time,
            "month_tokens": month_tokens_total,
            "month_cost": month_cost_total,
            "tokens_all_time_target": tokens_all_time_target,
            "tokens_all_time_suffix": tokens_all_time_suffix,
            "cost_all_time_display": f"${cost_all_time:,.2f}",
            "month_tokens_target": month_tokens_target,
            "month_tokens_suffix": month_tokens_suffix,
            "month_cost_display": f"${month_cost_total:,.2f}",
            "model_cost_rows": _bar_rows(model_labels, model_cost),
            "role_donut": _donut_segments(role_labels, role_values),
            "enabled_model_count": ModelConfig.objects.filter(is_enabled=True).count(),
            # Passed as plain Python values, not pre-`json.dumps`'d strings —
            # rendered via the `json_script` template filter (not `|safe`)
            # so admin-controlled free text (e.g. a model's display name)
            # can never break out of the inline <script> block below.
            "chart_daily_labels": daily_labels,
            "chart_daily_tokens": daily_tokens,
            "chart_daily_cost": daily_cost,
            "chart_model_labels": model_labels,
            "chart_model_cost": model_cost,
            "chart_role_labels": role_labels,
            "chart_role_values": role_values,
            "has_cost_data": any(daily_cost),
            "has_token_data": any(daily_tokens),
            "has_model_data": bool(model_labels),
            "org_usage": _org_usage_overview(scoped_users),
            "is_department_scoped": _is_scoped_admin(self.request.user),
            "pending_upgrade_requests": pending_requests_qs.count(),
            "recent_upgrade_requests": recent_upgrade_requests,
            "spark_tokens": _spark(daily_tokens),
            "spark_cost": _spark(daily_cost),
            "greeting_time": greeting_time,
            "greeting_name": greeting_name,
        }


class UserListView(FilterableListMixin, AdminRequiredMixin, ListView):
    model = User
    template_name = "governance/users.html"
    partial_template_name = "governance/_users_table.html"
    context_object_name = "users"

    def get_queryset(self):
        qs = User.objects.select_related("department", "team", "plan_assignment__plan").order_by("email")
        qs = _scope_users(self.request, qs)
        search = self.request.GET.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(email__icontains=search) | Q(first_name__icontains=search) | Q(last_name__icontains=search)
            )
        role = self.request.GET.get("role", "")
        if role in User.Role.values:
            qs = qs.filter(role=role)
        status = self.request.GET.get("status", "")
        if status == "active":
            qs = qs.filter(is_active=True)
        elif status == "suspended":
            qs = qs.filter(is_active=False)
        plan_id = self.request.GET.get("plan", "")
        if plan_id.isdigit():
            qs = qs.filter(plan_assignment__plan_id=plan_id)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        users = list(ctx[self.context_object_name])

        # One aggregated query for the whole page's "Requests (30d)" column,
        # not one query per row - counts the user's own messages (not the
        # assistant's replies) sent in the last 30 days.
        thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
        request_counts = dict(
            Message.objects.filter(
                role=Message.Role.USER, created_at__gte=thirty_days_ago, conversation__user__in=users
            )
            .values("conversation__user_id")
            .annotate(count=Count("id"))
            .values_list("conversation__user_id", "count")
        )

        plan_info = {}
        for u in users:
            status = get_plan_status(u)
            plan_info[u.id] = {
                **status,
                "engagement": engagement_score(u),
                "override_count": count_user_overrides(u),
                "request_count_30d": request_counts.get(u.id, 0),
            }

        if self.request.GET.get("sort") == "engagement":
            users.sort(key=lambda u: plan_info[u.id]["engagement"] or -1, reverse=True)
            ctx[self.context_object_name] = users

        assignable_roles, teams_qs, departments_qs = _assignable_roles_and_scope(self.request)

        return ctx | {
            "roles": User.Role.choices,
            "assignable_roles": assignable_roles,
            "assignable_roles_values": [v for v, _l in assignable_roles],
            "departments": departments_qs,
            "teams": teams_qs,
            "plans": Plan.objects.filter(is_active=True, is_visible_to_admins=True).order_by("name"),
            "plan_info": plan_info,
            "search": self.request.GET.get("search", ""),
            "role_filter": self.request.GET.get("role", ""),
            "status_filter": self.request.GET.get("status", ""),
            "plan_filter": self.request.GET.get("plan", ""),
            "sort": self.request.GET.get("sort", ""),
            "total_count": _scope_users(self.request, User.objects.all()).count(),
        }


@role_required(User.Role.ADMIN)
@require_GET
def user_edit_form(request, user_id):
    """Modal body for the Users list's "Edit" button - every write action
    for one user (role, department, team, email, password reset, suspend/
    activate) in one place, instead of the per-row popover this replaced.
    Each field still POSTs to its own existing endpoint, unchanged."""
    target = _get_scoped_user_or_403(request, user_id)
    assignable_roles, teams_qs, departments_qs = _assignable_roles_and_scope(request)
    context = {
        "u": target,
        "assignable_roles": assignable_roles,
        "assignable_roles_values": [v for v, _l in assignable_roles],
        "departments": departments_qs,
        "teams": teams_qs,
        "override_count": count_user_overrides(target),
    }
    return render(request, "governance/_user_edit_form.html", context)


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def toggle_user_active(request, user_id):
    target = _get_scoped_user_or_403(request, user_id)
    old_value = target.is_active
    target.is_active = not target.is_active
    target.save(update_fields=["is_active"])
    log_action(
        request.user,
        "user.suspend" if not target.is_active else "user.activate",
        target,
        old_value=old_value,
        new_value=target.is_active,
    )
    return redirect("governance:users")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def change_user_email(request, user_id):
    target = _get_scoped_user_or_403(request, user_id)
    new_email = request.POST.get("email", "").strip().lower()
    if not new_email:
        return HttpResponseBadRequest("Email is required")
    try:
        validate_email(new_email)
    except ValidationError:
        django_messages.error(request, _("%(email)s isn't a valid email address.") % {"email": new_email})
        return redirect("governance:users")
    if User.objects.exclude(id=target.id).filter(email__iexact=new_email).exists():
        django_messages.error(request, _("%(email)s is already in use by another account.") % {"email": new_email})
        return redirect("governance:users")

    old_value = target.email
    target.email = new_email
    target.save(update_fields=["email"])
    log_action(request.user, "user.email_change", target, old_value=old_value, new_value=new_email)
    django_messages.success(request, _("Email for %(old)s updated to %(new)s.") % {"old": old_value, "new": new_email})
    return redirect("governance:users")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def reset_user_password(request, user_id):
    """Admin sets a new password directly - no email/reset-link
    infrastructure needed. Uses Django's own SetPasswordForm so the same
    AUTH_PASSWORD_VALIDATORS rules apply as everywhere else in the app; the
    raw password itself is never written to the audit log, only the fact
    that a reset happened."""
    target = _get_scoped_user_or_403(request, user_id)
    form = SetPasswordForm(user=target, data=request.POST)
    if not form.is_valid():
        for field_errors in form.errors.values():
            for error in field_errors:
                django_messages.error(request, error)
        return redirect("governance:users")

    form.save()
    log_action(request.user, "user.password_reset", target)
    django_messages.success(request, _("Password reset for %(email)s.") % {"email": target.email})
    return redirect("governance:users")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def change_user_role(request, user_id):
    """Two-step confirm, mirroring change_user_plan's downgrade-confirm
    pattern below: a POST missing what's needed (a picked Team/Department,
    or the final `confirmed=1`) re-renders the same confirmation partial
    instead of applying anything — a role change never takes effect on the
    first click, matching the plan-downgrade confirmation's level of care.

    Permission rules (role hierarchy prompt, Section 1B): a SuperAdmin can
    set anyone to any role. A scoped Admin can only toggle between User and
    Manager, only for users already in their own department (enforced by
    _get_scoped_user_or_403 above) — never create another Admin or a
    SuperAdmin, which would let an Admin escalate their own department's
    access beyond what was granted to them.
    """
    target = _get_scoped_user_or_403(request, user_id)
    new_role = request.POST.get("role")
    if new_role not in User.Role.values:
        return HttpResponseBadRequest("Invalid role")
    if _is_scoped_admin(request.user) and new_role not in (User.Role.USER, User.Role.MANAGER):
        raise PermissionDenied("Only a SuperAdmin can grant Admin or SuperAdmin access.")

    confirmed = request.POST.get("confirmed") == "1"
    new_role_label = dict(User.Role.choices).get(new_role, new_role)
    team = None
    department = None

    if new_role == User.Role.MANAGER:
        # A Manager without an assigned team has nothing to see — never
        # allow saving that combination, always require a team first.
        team_id = request.POST.get("team_id")
        team = (
            _scope_teams(request, Team.objects.select_related("department")).filter(id=team_id).first()
            if team_id
            else None
        )
        if not team or not confirmed:
            return render(
                request,
                "governance/_role_change_confirm.html",
                {
                    "target": target,
                    "new_role": new_role,
                    "new_role_label": new_role_label,
                    "teams": _scope_teams(request, Team.objects.select_related("department")),
                    "selected_team_id": team.id if team else None,
                },
            )
    elif new_role == User.Role.ADMIN:
        # SuperAdmin-only (checked above), but a department-scoped Admin
        # must have exactly one department — never allow saving without one.
        department_id = request.POST.get("department_id")
        department = Department.objects.filter(id=department_id).first() if department_id else None
        if not department or not confirmed:
            return render(
                request,
                "governance/_role_change_confirm.html",
                {
                    "target": target,
                    "new_role": new_role,
                    "new_role_label": new_role_label,
                    "departments": Department.objects.order_by("name"),
                    "selected_department_id": department.id if department else None,
                },
            )
    elif not confirmed:
        return render(
            request,
            "governance/_role_change_confirm.html",
            {"target": target, "new_role": new_role, "new_role_label": new_role_label},
        )

    old_value = target.role
    with transaction.atomic():
        # Demoting away from Manager releases the team they used to manage
        # so it doesn't keep pointing at someone who's no longer a Manager.
        if target.role == User.Role.MANAGER and new_role != User.Role.MANAGER:
            Team.objects.filter(manager=target).update(manager=None)
            target.team = None

        target.role = new_role
        if new_role == User.Role.MANAGER:
            Team.objects.filter(manager=target).exclude(pk=team.pk).update(manager=None)
            # If the picked team already has a different Manager, release
            # them too - otherwise they'd keep `team` set while no longer
            # being that team's actual manager, stuck exactly the way a
            # Manager promoted outside this flow can end up (see
            # change_user_team, which fixes that same class of problem).
            previous_manager = team.manager
            if previous_manager is not None and previous_manager != target:
                previous_manager.team = None
                previous_manager.save(update_fields=["team"])
            target.team = team
            target.department_id = team.department_id
            team.manager = target
            team.save(update_fields=["manager"])
        elif new_role == User.Role.ADMIN:
            target.department = department

        target.save()

    log_action(request.user, "user.role_change", target, old_value=old_value, new_value=new_role)
    _notify_admin_change(target, f"Your role was changed to {target.get_role_display()}.")
    return redirect("governance:users")


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def change_user_department(request, user_id):
    """Moving a user between departments is department-structure
    management, which is SuperAdmin-only per the role hierarchy prompt's
    Section 1 ("The only role that can... manage departments") — a scoped
    Admin manages the *users* within their department, not which
    department a user belongs to."""
    target = get_object_or_404(User, id=user_id)
    department_id = request.POST.get("department_id") or None
    old_value = target.department_id
    target.department_id = department_id
    target.save(update_fields=["department"])
    log_action(request.user, "user.department_change", target, old_value=old_value, new_value=department_id)
    return redirect("governance:users")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def change_user_team(request, user_id):
    """Adds/removes a User's team, covering both meanings that field can
    have: a regular User's `team` is which team they're a MEMBER of, a
    Manager's is which team they MANAGE (Team.manager, kept in sync here
    the same way change_user_role does it on promotion).

    Also the only way to fix a Manager stuck teamless after being promoted
    outside this app's own flow (direct DB/admin-panel edit, a seed
    script, etc.) - re-selecting "Manager" from a <select> already showing
    "Manager" never fires a change event, so change_user_role's own
    team-picker can't be re-reached for someone already in that state
    without this."""
    target = _get_scoped_user_or_403(request, user_id)

    team_id = request.POST.get("team_id") or None
    team = _scope_teams(request, Team.objects.all()).filter(id=team_id).first() if team_id else None
    if team_id and team is None:
        raise PermissionDenied("That team is outside your scope.")

    old_value = target.team_id
    with transaction.atomic():
        if target.role == User.Role.MANAGER:
            # Release whatever team they used to manage before taking on a
            # new (or no) one - a Team can only ever have one Manager.
            Team.objects.filter(manager=target).exclude(pk=team.pk if team else None).update(manager=None)
            if team is not None:
                # If the picked team already has a different Manager,
                # release them too rather than leaving them stuck with
                # `team` set but no longer actually managing it.
                previous_manager = team.manager
                if previous_manager is not None and previous_manager != target:
                    previous_manager.team = None
                    previous_manager.save(update_fields=["team"])
                team.manager = target
                team.save(update_fields=["manager"])
        target.team = team
        # Department follows the team either way - keeps the two fields
        # from silently disagreeing about which department someone is in.
        if team is not None:
            target.department_id = team.department_id
            target.save(update_fields=["team", "department"])
        else:
            target.save(update_fields=["team"])
    log_action(request.user, "user.team_change", target, old_value=old_value, new_value=team_id)
    return redirect("governance:users")


def _notify_admin_change(user, body):
    from notifications.models import NotificationType
    from notifications.notify import notify

    notify(user, NotificationType.ADMIN_CHANGE, title="An admin updated your account", body=body)


def _notify_plan_change(user, plan):
    from notifications.models import NotificationType
    from notifications.notify import notify

    notify(
        user,
        NotificationType.PLAN_CHANGE,
        title="Your plan has changed",
        body=f"You're now on the {plan.name} plan.",
    )


def _usage_exceeds_plan(user, plan):
    """True if the user's usage so far already exceeds one of the target
    plan's caps - used to warn an admin before a downgrade takes effect,
    per the "don't leave this undefined" instruction: the rule here is the
    downgrade always proceeds (new usage is gated by the new plan
    immediately), this check only decides whether to show a confirmation
    step first."""
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    assistant_messages = Message.objects.filter(conversation__user=user, role=Message.Role.ASSISTANT)

    if plan.daily_token_limit is not None:
        used = (
            assistant_messages.filter(created_at__gte=today_start).aggregate(
                total=Sum(F("input_tokens") + F("output_tokens"))
            )["total"]
            or 0
        )
        if used > plan.daily_token_limit:
            return True
    if plan.monthly_token_limit is not None:
        used = (
            assistant_messages.filter(created_at__gte=month_start).aggregate(
                total=Sum(F("input_tokens") + F("output_tokens"))
            )["total"]
            or 0
        )
        if used > plan.monthly_token_limit:
            return True
    if plan.monthly_budget_cap is not None:
        spent = (
            assistant_messages.filter(created_at__gte=month_start).aggregate(total=Sum("estimated_cost"))["total"] or 0
        )
        if spent > plan.monthly_budget_cap:
            return True
    return False


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def change_user_plan(request, user_id):
    target = _get_scoped_user_or_403(request, user_id)
    plan = get_object_or_404(Plan, id=request.POST.get("plan_id"))
    confirmed = request.POST.get("confirmed") == "1"

    if not confirmed and _usage_exceeds_plan(target, plan):
        return render(request, "governance/_plan_downgrade_confirm.html", {"target": target, "plan": plan})

    old_assignment = get_assignment(target)
    old_plan_name = old_assignment.plan.name if old_assignment else "—"
    assign_plan(target, plan, assigned_by=request.user)
    log_action(request.user, "user.plan_change", target, old_value=old_plan_name, new_value=plan.name)
    _notify_plan_change(target, plan)
    return redirect("governance:users")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def bulk_change_plan(request):
    user_ids = request.POST.getlist("user_ids")
    plan_id = request.POST.get("plan_id")
    if not user_ids or not plan_id:
        django_messages.warning(request, _("Select at least one user and a plan first."))
        return redirect("governance:users")

    plan = get_object_or_404(Plan, id=plan_id)
    # Scoped to the acting Admin's own department, same as the Users list
    # itself — a manipulated user_ids list can't reach another department's
    # users this way, it just silently has no effect on ids outside scope.
    for target in _scope_users(request, User.objects.filter(id__in=user_ids)):
        old_assignment = get_assignment(target)
        old_plan_name = old_assignment.plan.name if old_assignment else "—"
        assign_plan(target, plan, assigned_by=request.user)
        log_action(request.user, "user.plan_change", target, old_value=old_plan_name, new_value=f"{plan.name} (bulk)")
        _notify_plan_change(target, plan)
    django_messages.success(
        request,
        ngettext(
            "Assigned %(plan)s to %(count)s user.",
            "Assigned %(plan)s to %(count)s users.",
            len(user_ids),
        )
        % {"plan": plan.name, "count": len(user_ids)},
    )
    return redirect("governance:users")


class UserOverridesView(AdminRequiredMixin, TemplateView):
    """Per-user view of the personal UsageLimit/UserModelPermission rows
    that override their Plan (see governance/plans.py's precedence rules) -
    what the "N custom overrides" badge on the Users list links to."""

    template_name = "governance/user_overrides.html"

    def get_context_data(self, **kwargs):
        target = _get_scoped_user_or_403(self.request, kwargs["user_id"])
        return super().get_context_data(**kwargs) | {
            "target": target,
            "overrides": get_user_overrides(target),
            "plan_status": get_plan_status(target),
        }


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def clear_user_overrides_view(request, user_id):
    target = _get_scoped_user_or_403(request, user_id)
    if count_user_overrides(target) == 0:
        django_messages.warning(request, _("%(email)s has no overrides to clear.") % {"email": target.email})
        return redirect("governance:users")

    cleared_description = clear_user_overrides(target)
    log_action(request.user, "user.overrides_cleared", target, old_value=cleared_description, new_value="")
    django_messages.success(
        request,
        _("Cleared overrides for %(email)s: %(cleared)s.") % {"email": target.email, "cleared": cleared_description},
    )
    return redirect("governance:users")


class PlanListView(SuperAdminRequiredMixin, ListView):
    """Plan Management is SuperAdmin-only (role hierarchy prompt, Section
    1/2) — an Admin uses the Plans a SuperAdmin already made visible to
    them (via Change Plan on the Users list), but cannot see this
    create/edit screen at all, not just have it grayed out."""

    model = Plan
    template_name = "governance/plans.html"
    context_object_name = "plans"
    queryset = Plan.objects.order_by("-is_default", "name")


class PlanFormView(SuperAdminRequiredMixin, TemplateView):
    template_name = "governance/plan_form.html"

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["governance/_plan_form_fields.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        plan = get_object_or_404(Plan, id=kwargs["plan_id"]) if kwargs.get("plan_id") else None
        existing_flags = plan.feature_flags if plan else {}
        return super().get_context_data(**kwargs) | {
            "plan": plan,
            "models": ModelConfig.objects.order_by("provider", "model_name"),
            "selected_model_ids": set(plan.allowed_models.values_list("id", flat=True)) if plan else set(),
            "known_flags": [(key, label, bool(existing_flags.get(key))) for key, label in KNOWN_FEATURE_FLAGS],
            "period_choices": Plan.Period.choices,
        }

    def post(self, request, plan_id=None):
        plan = get_object_or_404(Plan, id=plan_id) if plan_id else Plan()
        is_new = plan.pk is None

        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponseBadRequest("Name is required")

        plan.name = name
        plan.description = request.POST.get("description", "").strip()
        plan.is_demo = request.POST.get("is_demo") == "on"
        plan.demo_duration_days = _int_or_none(request.POST.get("demo_duration_days"))
        plan.daily_token_limit = _int_or_none(request.POST.get("daily_token_limit"))
        plan.monthly_token_limit = _int_or_none(request.POST.get("monthly_token_limit"))
        plan.messages_per_session_limit = _int_or_none(request.POST.get("messages_per_session_limit"))
        plan.sessions_per_day_limit = _int_or_none(request.POST.get("sessions_per_day_limit"))
        plan.monthly_budget_cap = _parse_decimal(request.POST.get("monthly_budget_cap"))
        plan.max_requests_per_period = _int_or_none(request.POST.get("max_requests_per_period"))
        plan.period = request.POST.get("period") or None
        plan.max_context_tokens = _int_or_none(request.POST.get("max_context_tokens"))
        plan.feature_flags = {key: request.POST.get(f"flag_{key}") == "on" for key, _label in KNOWN_FEATURE_FLAGS}
        plan.is_active = request.POST.get("is_active") == "on"
        plan.is_visible_to_admins = request.POST.get("is_visible_to_admins") == "on"

        make_default = request.POST.get("is_default") == "on"

        with transaction.atomic():
            plan.save()
            plan.allowed_models.set(request.POST.getlist("model_ids"))
            if make_default:
                Plan.objects.exclude(pk=plan.pk).update(is_default=False)
                plan.is_default = True
                plan.save(update_fields=["is_default"])
            elif plan.is_default and not make_default:
                plan.is_default = False
                plan.save(update_fields=["is_default"])

        log_action(request.user, "plan.create" if is_new else "plan.update", plan, new_value=plan.name)
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse("governance:plans")
            return response
        return redirect("governance:plans")


class UpgradeRequestListView(AdminRequiredMixin, RequireFeatureMixin, ListView):
    feature_key = "upgrade_requests"
    model = UpgradeRequest
    template_name = "governance/upgrade_requests.html"
    context_object_name = "upgrade_requests"

    def get_queryset(self):
        qs = UpgradeRequest.objects.select_related("user", "current_plan").filter(
            status=UpgradeRequest.Status.PENDING,
        )
        return _scope_by_user_department(self.request, qs)


@role_required(User.Role.ADMIN)
@require_feature("upgrade_requests")
@require_http_methods(["POST"])
def resolve_upgrade_request(request, request_id):
    upgrade_request = get_object_or_404(UpgradeRequest, id=request_id)
    if _is_scoped_admin(request.user) and upgrade_request.user.department_id != request.user.department_id:
        raise PermissionDenied("That upgrade request is outside your department.")
    action = request.POST.get("action")

    upgrade_request.resolved_at = timezone.now()
    upgrade_request.resolved_by = request.user

    if action == "approve":
        upgrade_request.status = UpgradeRequest.Status.APPROVED
        upgrade_request.save()
        log_action(request.user, "upgrade_request.approve", upgrade_request)
        if request.headers.get("HX-Request"):
            # A 204 tells htmx "success, don't swap" (its documented
            # behavior) - an empty 200 body is what actually makes the
            # outerHTML swap remove the <tr> from the DOM.
            return HttpResponse("")
        return redirect(f"{reverse('governance:users')}?search={upgrade_request.user.email}")

    upgrade_request.status = UpgradeRequest.Status.DISMISSED
    upgrade_request.save()
    log_action(request.user, "upgrade_request.dismiss", upgrade_request)
    if request.headers.get("HX-Request"):
        return HttpResponse("")
    return redirect("governance:upgrade_requests")


class ModelListView(FilterableListMixin, SuperAdminRequiredMixin, ListView):
    """Enabling/disabling models system-wide is SuperAdmin-only (role
    hierarchy prompt, Section 1) — an Admin applies whatever models a Plan
    already grants (via Change Plan), but never sees this screen."""

    model = ModelConfig
    template_name = "governance/models.html"
    partial_template_name = "governance/_models_table.html"
    context_object_name = "models"

    def get_queryset(self):
        qs = ModelConfig.objects.order_by("provider", "tier", "model_name")
        search = self.request.GET.get("search", "").strip()
        if search:
            qs = qs.filter(Q(model_name__icontains=search) | Q(provider__icontains=search))
        status = self.request.GET.get("status", "")
        if status == "enabled":
            qs = qs.filter(is_enabled=True)
        elif status == "disabled":
            qs = qs.filter(is_enabled=False)
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {
            "search": self.request.GET.get("search", ""),
            "status_filter": self.request.GET.get("status", ""),
            "total_count": ModelConfig.objects.count(),
        }


def _models_table_context(request):
    """Shared with ModelListView.get_queryset/get_context_data so the
    toggle endpoint's htmx re-render respects the same search/status
    filters the admin was already looking at, without duplicating pagination
    or other ListView machinery just for this one partial."""
    qs = ModelConfig.objects.order_by("provider", "tier", "model_name")
    search = request.GET.get("search", "").strip()
    if search:
        qs = qs.filter(Q(model_name__icontains=search) | Q(provider__icontains=search))
    status = request.GET.get("status", "")
    if status == "enabled":
        qs = qs.filter(is_enabled=True)
    elif status == "disabled":
        qs = qs.filter(is_enabled=False)
    return {"models": qs, "total_count": ModelConfig.objects.count(), "search": search, "status_filter": status}


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def toggle_model_enabled(request, model_id):
    model_config = get_object_or_404(ModelConfig, id=model_id)
    old_value = model_config.is_enabled
    model_config.is_enabled = not model_config.is_enabled
    model_config.save(update_fields=["is_enabled"])
    log_action(
        request.user,
        "model.enable" if model_config.is_enabled else "model.disable",
        model_config,
        old_value=old_value,
        new_value=model_config.is_enabled,
    )
    if request.headers.get("HX-Request"):
        return render(request, "governance/_models_table.html", _models_table_context(request))
    return redirect("governance:models")


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def delete_model(request, model_id):
    """The sync-models flow (Add model -> pick from fetched list -> Import)
    had no way to undo an accidental/test import - Disable just hides a
    model from chat, it stays in this list forever. Real deletion, but
    only when nothing actually depends on it: Message.model_used is
    on_delete=SET_NULL (so deleting wouldn't crash), but silently nulling
    out which model a real historical message used is real data loss, not
    cleanup - blocked here rather than allowed silently."""
    model_config = get_object_or_404(ModelConfig, id=model_id)
    if model_config.messages.exists():
        django_messages.error(
            request,
            _("%(model)s has real usage history and can't be deleted - disable it instead.")
            % {"model": model_config.display_label},
        )
    else:
        log_action(request.user, "model.delete", model_config, old_value=model_config.display_label)
        model_config.delete()
    if request.headers.get("HX-Request"):
        return render(request, "governance/_models_table.html", _models_table_context(request))
    return redirect("governance:models")


def _parse_decimal(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _int_or_none(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def update_model_pricing(request, model_id):
    model_config = get_object_or_404(ModelConfig, id=model_id)
    old_value = f"in={model_config.input_cost_per_1m} out={model_config.output_cost_per_1m}"
    model_config.input_cost_per_1m = _parse_decimal(request.POST.get("input_cost_per_1m"))
    model_config.output_cost_per_1m = _parse_decimal(request.POST.get("output_cost_per_1m"))
    model_config.display_name = request.POST.get("display_name", "").strip()
    model_config.save(update_fields=["input_cost_per_1m", "output_cost_per_1m", "display_name"])
    log_action(
        request.user,
        "model.pricing_update",
        model_config,
        old_value=old_value,
        new_value=f"in={model_config.input_cost_per_1m} out={model_config.output_cost_per_1m}",
    )
    return redirect("governance:models")


class ModelPermissionsView(SuperAdminRequiredMixin, TemplateView):
    template_name = "governance/model_permissions.html"

    def get_context_data(self, **kwargs):
        model_config = get_object_or_404(ModelConfig, id=kwargs["model_id"])
        denied_user_ids = set(
            UserModelPermission.objects.filter(model_config=model_config, is_allowed=False).values_list(
                "user_id", flat=True
            )
        )
        return super().get_context_data(**kwargs) | {
            "model_config": model_config,
            "users": User.objects.order_by("email"),
            "denied_user_ids": denied_user_ids,
        }

    def post(self, request, model_id):
        model_config = get_object_or_404(ModelConfig, id=model_id)
        target = get_object_or_404(User, id=request.POST.get("user_id"))
        _toggle_user_model_permission(request.user, target, model_config)
        return redirect("governance:model_permissions", model_id=model_config.id)


def _message_model_label(message):
    """Which model a message used, reading whichever of the two fields is
    actually set - provider_model_used for every message since the
    ModelConfig -> ProviderModel cutover (see chat/providers.py), model_used
    for everything before it. Used by the CSV/XLSX usage exports so the
    Model column doesn't just go blank for every message from here on."""
    if message.provider_model_used_id:
        return message.provider_model_used.model_id
    if message.model_used_id:
        return message.model_used.model_name
    return ""


def _filtered_assistant_messages(request):
    """Shared by the Usage & Cost screen and its CSV/XLSX exports, so
    "export respects whatever filter is active" is true by construction -
    both read the exact same query, not two independently-maintained ones.
    Department-scoping lives here too for the same reason — a scoped Admin
    can't get another department's usage data through the CSV/XLSX export
    even though the on-screen table is scoped, since all three read this."""
    assistant_messages = Message.objects.filter(role=Message.Role.ASSISTANT)
    assistant_messages = _scope_by_user_department(request, assistant_messages, "conversation__user__department_id")

    search = request.GET.get("search", "").strip()
    if search:
        assistant_messages = assistant_messages.filter(conversation__user__email__icontains=search)

    model_id = request.GET.get("model", "").strip()
    if model_id.isdigit():
        assistant_messages = assistant_messages.filter(model_used_id=model_id)

    date_from = request.GET.get("date_from", "").strip()
    if date_from:
        assistant_messages = assistant_messages.filter(created_at__date__gte=date_from)
    date_to = request.GET.get("date_to", "").strip()
    if date_to:
        assistant_messages = assistant_messages.filter(created_at__date__lte=date_to)

    return assistant_messages, {"search": search, "model_id": model_id, "date_from": date_from, "date_to": date_to}


class UsageSummaryView(FilterableListMixin, AdminRequiredMixin, RequireFeatureMixin, TemplateView):
    feature_key = "usage_cost"
    template_name = "governance/usage.html"
    partial_template_name = "governance/_usage_table.html"

    def get_context_data(self, **kwargs):
        assistant_messages, filters = _filtered_assistant_messages(self.request)

        # Independent of the table's own search/model/date filters above -
        # a stable "last 7 days" trend, scoped the same way (department)
        # as the rest of the page but not narrowed by whatever the admin
        # happens to have typed into the filter row. Same zero-fill shape
        # as DashboardView's 14-day series, just a shorter window.
        today = timezone.localdate()
        seven_days_ago = today - timezone.timedelta(days=6)
        week_scope = _scope_by_user_department(
            self.request,
            Message.objects.filter(role=Message.Role.ASSISTANT, created_at__date__gte=seven_days_ago),
            "conversation__user__department_id",
        )
        weekly_rows = (
            week_scope.annotate(day=TruncDate("created_at")).values("day").annotate(cost=Sum("estimated_cost"))
        )
        cost_by_day = {row["day"]: float(row["cost"] or 0) for row in weekly_rows}
        week_range = [seven_days_ago + timezone.timedelta(days=i) for i in range(7)]
        weekly_cost = [cost_by_day.get(d, 0.0) for d in week_range]
        weekly_labels = [d.strftime("%a") for d in week_range]
        peak_weekly_cost = max(weekly_cost) or 1
        weekly_cost_pct = [round(c / peak_weekly_cost * 100) for c in weekly_cost]

        per_user = (
            assistant_messages.values("conversation__user__email")
            .annotate(
                requests=Count("id"),
                tokens=Sum(F("input_tokens") + F("output_tokens")),
                cost=Sum("estimated_cost"),
            )
            .order_by("-cost")
        )
        # Cache hit rate / cost saved (see chat/response_cache.py) - over
        # whatever filter is active, same as everything else on this page.
        cache_stats = assistant_messages.aggregate(
            total=Count("id"),
            cache_hits=Count("id", filter=Q(served_from_cache=True)),
            cost_saved=Sum("estimated_cost", filter=Q(served_from_cache=True)),
        )
        cache_hit_rate = round((cache_stats["cache_hits"] / cache_stats["total"]) * 100) if cache_stats["total"] else 0

        return super().get_context_data(**kwargs) | {
            "per_user": per_user,
            "models": ModelConfig.objects.order_by("provider", "model_name"),
            "search": filters["search"],
            "model_filter": filters["model_id"],
            "date_from": filters["date_from"],
            "date_to": filters["date_to"],
            "cache_hits": cache_stats["cache_hits"],
            "cache_hit_rate": cache_hit_rate,
            "cache_cost_saved": cache_stats["cost_saved"] or 0,
            "weekly_bars": list(zip(weekly_labels, weekly_cost, weekly_cost_pct)),
            "has_weekly_data": any(weekly_cost),
        }


@role_required(User.Role.ADMIN)
@require_feature("usage_cost")
@require_GET
def export_usage_csv(request):
    import csv

    assistant_messages, _ = _filtered_assistant_messages(request)
    rows = assistant_messages.select_related(
        "conversation__user__department", "model_used", "provider_model_used"
    ).order_by("created_at")

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="usage_export.csv"'
    writer = csv.writer(response)
    writer.writerow(["Date", "User", "Department", "Model", "Input tokens", "Output tokens", "Estimated cost"])
    for m in rows:
        writer.writerow(
            [
                m.created_at.strftime("%Y-%m-%d %H:%M"),
                m.conversation.user.email,
                m.conversation.user.department.name if m.conversation.user.department else "",
                _message_model_label(m),
                m.input_tokens or 0,
                m.output_tokens or 0,
                f"{m.estimated_cost or 0:.6f}",
            ]
        )
    return response


@role_required(User.Role.ADMIN)
@require_feature("usage_cost")
@require_GET
def export_usage_xlsx(request):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    assistant_messages, _ = _filtered_assistant_messages(request)
    rows = assistant_messages.select_related(
        "conversation__user__department", "model_used", "provider_model_used"
    ).order_by("created_at")

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Usage"
    headers = ["Date", "User", "Department", "Model", "Input tokens", "Output tokens", "Estimated cost"]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for m in rows:
        sheet.append(
            [
                m.created_at.strftime("%Y-%m-%d %H:%M"),
                m.conversation.user.email,
                m.conversation.user.department.name if m.conversation.user.department else "",
                _message_model_label(m),
                m.input_tokens or 0,
                m.output_tokens or 0,
                float(m.estimated_cost or 0),
            ]
        )
    for column_cells in sheet.columns:
        length = (
            max(len(str(cell.value)) for cell in column_cells if cell.value is not None)
            if any(cell.value is not None for cell in column_cells)
            else 10
        )
        sheet.column_dimensions[column_cells[0].column_letter].width = min(max(length + 2, 10), 40)

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="usage_export.xlsx"'
    workbook.save(response)
    return response


@role_required(User.Role.SUPERADMIN, exact=True)
@require_GET
def export_usage_monthly_summary(request):
    """Aggregates ACROSS every department for one calendar month, by
    design (that's the point of a chargeback summary) — a department-scoped
    Admin seeing other departments' costs broken out would defeat the whole
    scoping model, so this one export is SuperAdmin-only rather than
    department-scoped like the rest of Usage & Cost. `month` is "YYYY-MM";
    defaults to the current month."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    month_param = request.GET.get("month", "").strip()
    if month_param:
        try:
            year, month = (int(part) for part in month_param.split("-"))
        except ValueError:
            return HttpResponseBadRequest("month must be in YYYY-MM format")
    else:
        today = timezone.localdate()
        year, month = today.year, today.month

    month_start = timezone.datetime(year, month, 1, tzinfo=timezone.get_current_timezone())
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    month_end = timezone.datetime(next_year, next_month, 1, tzinfo=timezone.get_current_timezone())

    by_department = (
        Message.objects.filter(role=Message.Role.ASSISTANT, created_at__gte=month_start, created_at__lt=month_end)
        .values("conversation__user__department__name")
        .annotate(
            tokens=Sum(F("input_tokens") + F("output_tokens")),
            cost=Sum("estimated_cost"),
        )
        .order_by("-cost")
    )

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = f"{year}-{month:02d} by department"
    sheet.append(["Department", "Total tokens", "Total cost"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in by_department:
        sheet.append(
            [
                row["conversation__user__department__name"] or "(no department)",
                row["tokens"] or 0,
                float(row["cost"] or 0),
            ]
        )
    for column_cells in sheet.columns:
        length = max((len(str(c.value)) for c in column_cells if c.value is not None), default=10)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(max(length + 2, 12), 40)

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="usage_summary_{year}-{month:02d}.xlsx"'
    workbook.save(response)
    return response


class AuditLogListView(FilterableListMixin, AdminRequiredMixin, RequireFeatureMixin, ListView):
    feature_key = "audit_logs"
    model = AuditLog
    template_name = "governance/audit_logs.html"
    partial_template_name = "governance/_audit_logs_table.html"
    context_object_name = "logs"
    paginate_by = 50

    def get_queryset(self):
        qs = _scope_audit_logs(self.request, AuditLog.objects.select_related("actor"))
        search = self.request.GET.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(actor__email__icontains=search) | Q(action_type__icontains=search) | Q(target_type__icontains=search)
            )
        action_type = self.request.GET.get("action_type", "").strip()
        if action_type:
            qs = qs.filter(action_type=action_type)
        date_from = self.request.GET.get("date_from", "").strip()
        if date_from:
            qs = qs.filter(timestamp__date__gte=date_from)
        date_to = self.request.GET.get("date_to", "").strip()
        if date_to:
            qs = qs.filter(timestamp__date__lte=date_to)
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {
            "search": self.request.GET.get("search", ""),
            "action_type_filter": self.request.GET.get("action_type", ""),
            "date_from": self.request.GET.get("date_from", ""),
            "date_to": self.request.GET.get("date_to", ""),
            "action_types": AuditLog.objects.values_list("action_type", flat=True).distinct().order_by("action_type"),
            "total_count": _scope_audit_logs(self.request, AuditLog.objects.all()).count(),
            "querystring_without_page": _querystring_without(self.request, "page"),
        }


class FeedbackListView(FilterableListMixin, AdminRequiredMixin, RequireFeatureMixin, ListView):
    """Response feedback (thumbs up/down) — visibility only, per spec: this
    never feeds back into model selection/routing automatically."""

    feature_key = "feedback"
    model = MessageFeedback
    template_name = "governance/feedback.html"
    partial_template_name = "governance/_feedback_table.html"
    context_object_name = "feedback_entries"
    paginate_by = 50

    def get_queryset(self):
        qs = MessageFeedback.objects.select_related(
            "user", "model_used", "provider_model_used", "message", "message__conversation"
        )
        qs = _scope_by_user_department(self.request, qs)
        search = self.request.GET.get("search", "").strip()
        if search:
            qs = qs.filter(Q(user__email__icontains=search) | Q(comment__icontains=search))
        rating = self.request.GET.get("rating", "").strip()
        if rating in MessageFeedback.Rating.values:
            qs = qs.filter(rating=rating)
        model_id = self.request.GET.get("model", "").strip()
        if model_id.isdigit():
            qs = qs.filter(model_used_id=model_id)
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {
            "search": self.request.GET.get("search", ""),
            "rating_filter": self.request.GET.get("rating", ""),
            "model_filter": self.request.GET.get("model", ""),
            "models": ModelConfig.objects.all(),
            "total_count": _scope_by_user_department(self.request, MessageFeedback.objects.all()).count(),
            "querystring_without_page": _querystring_without(self.request, "page"),
        }


class DepartmentListView(FilterableListMixin, SuperAdminRequiredMixin, ListView):
    """Managing departments is SuperAdmin-only (role hierarchy prompt,
    Section 1) — a department-scoped Admin operates *within* a department,
    they don't create/edit/delete departments themselves."""

    model = Department
    template_name = "governance/departments.html"
    partial_template_name = "governance/_departments_table.html"
    context_object_name = "departments"

    def get_queryset(self):
        qs = Department.objects.order_by("name")
        search = self.request.GET.get("search", "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {
            "search": self.request.GET.get("search", ""),
            "total_count": Department.objects.count(),
        }


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def add_department(request):
    name = request.POST.get("name", "").strip()
    if not name:
        return HttpResponseBadRequest("Name is required")
    budget_cap = _parse_decimal(request.POST.get("monthly_budget_cap"))
    department, created = Department.objects.get_or_create(
        name=name,
        defaults={"monthly_budget_cap": budget_cap},
    )
    if created:
        log_action(request.user, "department.add", department, new_value=name)
    return redirect("governance:departments")


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def update_department(request, department_id):
    department = get_object_or_404(Department, id=department_id)
    old_value = f"name={department.name} cap={department.monthly_budget_cap}"
    new_name = request.POST.get("name", "").strip()
    if not new_name:
        return HttpResponseBadRequest("Name is required")
    department.name = new_name
    department.monthly_budget_cap = _parse_decimal(request.POST.get("monthly_budget_cap"))
    department.save(update_fields=["name", "monthly_budget_cap"])
    log_action(
        request.user,
        "department.update",
        department,
        old_value=old_value,
        new_value=f"name={department.name} cap={department.monthly_budget_cap}",
    )
    return redirect("governance:departments")


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def delete_department(request, department_id):
    department = get_object_or_404(Department, id=department_id)
    log_action(request.user, "department.delete", department, old_value=department.name)
    department.delete()
    return redirect("governance:departments")


def _get_scoped_department_or_403(request, department_id):
    """Unlike department STRUCTURE (create/rename/delete — SuperAdmin-only,
    see Section 1), a department's system prompt and team templates are
    content an Admin operates within their own department, same footing as
    everything else in Section 2 — so this is scoped, not SuperAdmin-only."""
    department = get_object_or_404(Department, id=department_id)
    if _is_scoped_admin(request.user) and department.id != request.user.department_id:
        raise PermissionDenied("That department is outside your scope.")
    return department


class SystemPromptView(AdminRequiredMixin, RequireFeatureMixin, TemplateView):
    feature_key = "department_settings"
    template_name = "governance/system_prompt.html"

    def get_context_data(self, **kwargs):
        department = _get_scoped_department_or_403(self.request, kwargs["department_id"])
        return super().get_context_data(**kwargs) | {
            "department": department,
            "active_version": SystemPromptVersion.objects.filter(department=department, is_active=True).first(),
            "history": SystemPromptVersion.objects.filter(department=department).order_by("-created_at")[:10],
        }

    def post(self, request, department_id):
        department = _get_scoped_department_or_403(request, department_id)
        content = request.POST.get("content", "").strip()
        tone_preference = request.POST.get("tone_preference", SystemPromptVersion.Tone.FORMAL)
        restricted_topics = request.POST.get("restricted_topics", "").strip()

        if not content:
            return self.get(request, department_id=department_id)

        new_version = SystemPromptVersion.objects.create_new_version(
            department=department,
            content=content,
            tone_preference=tone_preference,
            restricted_topics=restricted_topics,
            created_by=request.user,
        )
        log_action(request.user, "system_prompt.new_version", new_version, old_value="", new_value=content[:200])
        return redirect("governance:system_prompt", department_id=department.id)


class DepartmentTemplatesView(AdminRequiredMixin, RequireFeatureMixin, TemplateView):
    """Team prompt templates for one department - visible to every user in
    that department alongside their own personal ones (see
    chat/views.py::_visible_prompt_templates)."""

    feature_key = "department_settings"
    template_name = "governance/department_templates.html"

    def get_context_data(self, **kwargs):
        department = _get_scoped_department_or_403(self.request, kwargs["department_id"])
        return super().get_context_data(**kwargs) | {
            "department": department,
            "templates": PromptTemplate.objects.filter(department=department),
        }

    def post(self, request, department_id):
        department = _get_scoped_department_or_403(request, department_id)
        name = request.POST.get("name", "").strip()
        content = request.POST.get("content", "").strip()
        if name and content:
            template = PromptTemplate.objects.create(department=department, name=name[:100], content=content)
            log_action(request.user, "prompt_template.create", template, new_value=name)
        return redirect("governance:department_templates", department_id=department.id)


@role_required(User.Role.ADMIN)
@require_feature("department_settings")
@require_http_methods(["POST"])
def delete_department_template(request, department_id, template_id):
    _get_scoped_department_or_403(request, department_id)
    template = get_object_or_404(PromptTemplate, id=template_id, department_id=department_id)
    log_action(request.user, "prompt_template.delete", template, old_value=template.name)
    template.delete()
    return redirect("governance:department_templates", department_id=department_id)


class LimitListView(FilterableListMixin, AdminRequiredMixin, RequireFeatureMixin, ListView):
    feature_key = "limits"
    model = UsageLimit
    template_name = "governance/limits.html"
    partial_template_name = "governance/_limits_table.html"
    context_object_name = "limits"

    def get_queryset(self):
        qs = _scope_limits(self.request, UsageLimit.objects.select_related("user", "department"))
        search = self.request.GET.get("search", "").strip()
        if search:
            qs = qs.filter(Q(user__email__icontains=search) | Q(department__name__icontains=search))
        return qs

    def get_context_data(self, **kwargs):
        from django.conf import settings

        return super().get_context_data(**kwargs) | {
            "default_upload_mb": settings.DEFAULT_MAX_UPLOAD_SIZE_MB,
            "default_extensions": settings.DEFAULT_ALLOWED_FILE_EXTENSIONS,
            "search": self.request.GET.get("search", ""),
            "total_count": _scope_limits(self.request, UsageLimit.objects.all()).count(),
        }


class LimitFormView(AdminRequiredMixin, RequireFeatureMixin, TemplateView):
    feature_key = "limits"
    template_name = "governance/limit_form.html"

    def get_context_data(self, **kwargs):
        limit = None
        if kwargs.get("limit_id"):
            limit = _scope_limits(self.request, UsageLimit.objects.filter(id=kwargs["limit_id"])).first()
            if limit is None:
                raise PermissionDenied("That limit is outside your scope.")
        return super().get_context_data(**kwargs) | {
            "limit": limit,
            "users": _scope_users(self.request, User.objects.order_by("email")),
            "departments": (
                Department.objects.filter(id=self.request.user.department_id)
                if _is_scoped_admin(self.request.user)
                else Department.objects.order_by("name")
            ),
        }

    def post(self, request, limit_id=None):
        if limit_id:
            limit = _scope_limits(request, UsageLimit.objects.filter(id=limit_id)).first()
            if limit is None:
                raise PermissionDenied("That limit is outside your scope.")
        else:
            limit = UsageLimit()

        target_type = request.POST.get("target_type")
        if target_type == "user":
            limit.user_id = request.POST.get("user_id") or None
            limit.department_id = None
        else:
            limit.department_id = request.POST.get("department_id") or None
            limit.user_id = None

        if not limit.user_id and not limit.department_id:
            return HttpResponseBadRequest("Pick a user or a department")

        # A scoped Admin can only ever target their own department or a
        # user within it — re-checked here (not just via the picker
        # querysets above) since POST data is attacker-controlled.
        if _is_scoped_admin(request.user):
            dept_id = request.user.department_id
            target_user = User.objects.filter(id=limit.user_id).first() if limit.user_id else None
            if (limit.user_id and (target_user is None or target_user.department_id != dept_id)) or (
                limit.department_id and str(limit.department_id) != str(dept_id)
            ):
                raise PermissionDenied("That target is outside your department.")

        def _int_or_none(key):
            raw = (request.POST.get(key) or "").strip()
            return int(raw) if raw.isdigit() else None

        limit.daily_token_cap = _int_or_none("daily_token_cap")
        limit.monthly_token_cap = _int_or_none("monthly_token_cap")
        limit.session_limit = _int_or_none("session_limit")
        limit.budget_cap_currency = _parse_decimal(request.POST.get("budget_cap_currency"))
        limit.max_upload_size_mb = _int_or_none("max_upload_size_mb")
        limit.allowed_file_extensions = request.POST.get("allowed_file_extensions", "").strip()

        is_new = limit.pk is None
        limit.save()
        log_action(
            request.user,
            "limit.create" if is_new else "limit.update",
            limit,
            new_value=f"user={limit.user_id} dept={limit.department_id}",
        )
        if limit.user_id:
            _notify_admin_change(limit.user, "An administrator updated your usage limits.")
        return redirect("governance:limits")


@role_required(User.Role.ADMIN)
@require_feature("limits")
@require_http_methods(["POST"])
def delete_limit(request, limit_id):
    limit = _scope_limits(request, UsageLimit.objects.filter(id=limit_id)).first()
    if limit is None:
        raise PermissionDenied("That limit is outside your scope.")
    log_action(request.user, "limit.delete", limit, old_value=str(limit))
    limit.delete()
    return redirect("governance:limits")


class TeamListView(FilterableListMixin, AdminRequiredMixin, RequireFeatureMixin, ListView):
    """Team management is NOT SuperAdmin-only like Plans/Models/Departments
    — a team is day-to-day department operations (who a Manager oversees),
    not a system-wide capability, so a department-scoped Admin needs to be
    able to create one before they can even promote someone to Manager
    (Section 1B requires a team to already exist to complete that action).
    An Admin manages only their own department's teams; a SuperAdmin
    manages any team in any department."""

    feature_key = "teams"
    model = Team
    template_name = "governance/teams.html"
    partial_template_name = "governance/_teams_table.html"
    context_object_name = "teams"

    def get_queryset(self):
        qs = _scope_teams(
            self.request,
            Team.objects.select_related("department", "manager").annotate(member_count=Count("members")),
        )
        search = self.request.GET.get("search", "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(department__name__icontains=search))
        return qs

    def get_context_data(self, **kwargs):
        departments = (
            Department.objects.filter(id=self.request.user.department_id)
            if _is_scoped_admin(self.request.user)
            else Department.objects.order_by("name")
        )
        return super().get_context_data(**kwargs) | {
            "departments": departments,
            "search": self.request.GET.get("search", ""),
            "total_count": _scope_teams(self.request, Team.objects.all()).count(),
        }


@role_required(User.Role.ADMIN)
@require_feature("teams")
@require_http_methods(["POST"])
def add_team(request):
    name = request.POST.get("name", "").strip()
    if not name:
        return HttpResponseBadRequest("Name is required")
    if _is_scoped_admin(request.user):
        department_id = request.user.department_id
        if not department_id:
            return HttpResponseBadRequest("You have no department assigned — ask a SuperAdmin to assign one first.")
    else:
        department_id = request.POST.get("department_id")
        if not department_id:
            # The form only shows a department picker when there's more than
            # one to choose from (see teams.html) - with exactly one, fall
            # back to it instead of demanding a field the UI never offered.
            only_department = Department.objects.values_list("id", flat=True)
            if only_department.count() == 1:
                department_id = only_department.first()
            else:
                return HttpResponseBadRequest("Department is required")
    team, created = Team.objects.get_or_create(name=name, department_id=department_id)
    if created:
        log_action(request.user, "team.add", team, new_value=name)
    return redirect("governance:teams")


@role_required(User.Role.ADMIN)
@require_feature("teams")
@require_http_methods(["POST"])
def delete_team(request, team_id):
    team = _scope_teams(request, Team.objects.filter(id=team_id)).first()
    if team is None:
        raise PermissionDenied("That team is outside your scope.")
    # Deleting a team shouldn't silently change anyone's role — members and
    # the former manager just lose their team reference, nothing else.
    User.objects.filter(team=team).update(team=None)
    log_action(request.user, "team.delete", team, old_value=team.name)
    team.delete()
    return redirect("governance:teams")


class ManagerDashboardView(ManagerRequiredMixin, TemplateView):
    """A Manager's own simplified, dashboard-only view — usage/activity for
    their team, plus one write action: which of the org's enabled models
    their team may use (toggle_team_model below). That's the one exception
    to the original "no write actions for Manager" design (role hierarchy
    prompt, Section 1) - everything else still routes to "reach out to your
    department's Admin". A team's model toggle can only ever narrow what
    the members' Plan already grants, never widen it - see
    governance/plans.py:effective_allowed_model_ids."""

    template_name = "governance/manager_dashboard.html"

    def get_context_data(self, **kwargs):
        team = getattr(self.request.user, "managed_team", None)
        if team is None:
            return super().get_context_data(**kwargs) | {"team": None, "members": [], "model_rows": []}

        members = User.objects.filter(team=team).order_by("email")
        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        assistant_messages = Message.objects.filter(role=Message.Role.ASSISTANT, conversation__user__team=team)
        month_messages = assistant_messages.filter(created_at__gte=month_start)

        member_stats = []
        for member in members:
            agg = assistant_messages.filter(conversation__user=member).aggregate(
                tokens=Sum(F("input_tokens") + F("output_tokens")), cost=Sum("estimated_cost")
            )
            member_stats.append(
                {
                    "user": member,
                    "tokens": agg["tokens"] or 0,
                    "cost": agg["cost"] or 0,
                    "engagement": engagement_score(member),
                }
            )

        disabled_ids = set(team.disabled_models.values_list("id", flat=True))
        model_rows = [
            {"model": mc, "disabled": mc.id in disabled_ids} for mc in ModelConfig.objects.filter(is_enabled=True)
        ]

        return super().get_context_data(**kwargs) | {
            "team": team,
            "members": member_stats,
            "model_rows": model_rows,
            "total_tokens_all_time": assistant_messages.aggregate(total=Sum(F("input_tokens") + F("output_tokens")))[
                "total"
            ]
            or 0,
            "total_cost_all_time": assistant_messages.aggregate(total=Sum("estimated_cost"))["total"] or 0,
            "month_tokens": month_messages.aggregate(total=Sum(F("input_tokens") + F("output_tokens")))["total"] or 0,
            "month_cost": month_messages.aggregate(total=Sum("estimated_cost"))["total"] or 0,
        }


@role_required(User.Role.MANAGER)
@require_http_methods(["POST"])
def toggle_team_model(request, model_id):
    """A Manager's one write action - restrict or restore one of the org's
    already-enabled models for their own team. Never touches Plan.allowed_
    models or any other team's scope; effective_allowed_model_ids() is what
    actually enforces this at request time (see governance/plans.py)."""
    team = getattr(request.user, "managed_team", None)
    if team is None:
        raise PermissionDenied("You don't manage a team.")
    model_config = get_object_or_404(ModelConfig, id=model_id, is_enabled=True)

    is_disabled = team.disabled_models.filter(id=model_config.id).exists()
    if is_disabled:
        team.disabled_models.remove(model_config)
    else:
        team.disabled_models.add(model_config)
    log_action(
        request.user,
        "team.model.enable" if is_disabled else "team.model.disable",
        model_config,
        old_value=team.name,
        new_value=model_config.display_label,
    )
    if request.headers.get("HX-Request"):
        disabled_ids = set(team.disabled_models.values_list("id", flat=True))
        model_rows = [
            {"model": mc, "disabled": mc.id in disabled_ids} for mc in ModelConfig.objects.filter(is_enabled=True)
        ]
        return render(request, "governance/_manager_model_rows.html", {"team": team, "model_rows": model_rows})
    return redirect("governance:manager_dashboard")


@role_required(User.Role.MANAGER, exact=True)
@require_http_methods(["POST"])
def remove_team_member(request, user_id):
    """A Manager's second write action: remove a member from their own
    team WITHOUT touching the account itself - login, Plan, personal
    UserModelPermission overrides, and history are all untouched, only
    team membership (and therefore this team's disabled_models scoping,
    and manager_member_permissions access below) is cleared. Everything
    else about "managing" a user's account (suspend, role, department,
    plan) stays exclusively an Admin/SuperAdmin action - unchanged by
    this addition."""
    target, team = _get_team_member_or_403(request, user_id)
    if target.id == request.user.id:
        return HttpResponseBadRequest("You can't remove yourself from your own team.")

    target.team = None
    target.save(update_fields=["team"])
    log_action(request.user, "team.member_removed", target, old_value=team.name, new_value="")
    django_messages.success(
        request, _("%(email)s was removed from %(team)s.") % {"email": target.email, "team": team.name}
    )
    return redirect("governance:manager_dashboard")


@role_required(User.Role.MANAGER, exact=True)
@require_GET
def manager_member_permissions(request, user_id):
    """Modal body: the admin-curated pool of manager-assignable models
    (ProviderModel.is_enabled=True and is_manager_assignable=True - an
    admin opts a model into this pool independently of any Plan, see
    providers/models.py), and which of them this one team member has
    actually been assigned. Grouped by provider so a Manager splitting a
    10-person team across Claude/GPT/Grok/DeepSeek can see at a glance
    which is which."""
    target, team = _get_team_member_or_403(request, user_id)
    assignable_models = ProviderModel.objects.filter(is_enabled=True, is_manager_assignable=True).select_related(
        "provider"
    )
    granted_ids = set(
        UserModelPermission.objects.filter(user=target, provider_model__isnull=False).values_list(
            "provider_model_id", flat=True
        )
    )
    context = {
        "target": target,
        "models": assignable_models,
        "granted_ids": granted_ids,
    }
    return render(request, "governance/_manager_member_permissions.html", context)


@role_required(User.Role.MANAGER, exact=True)
@require_http_methods(["POST"])
def toggle_member_model_permission(request, user_id, model_id):
    target, team = _get_team_member_or_403(request, user_id)
    provider_model = get_object_or_404(ProviderModel, id=model_id, is_enabled=True, is_manager_assignable=True)
    _toggle_manager_assigned_model(request.user, target, provider_model)
    if request.headers.get("HX-Request"):
        return manager_member_permissions(request, user_id)
    return redirect("governance:manager_dashboard")


class FeatureVisibilityView(SuperAdminRequiredMixin, TemplateView):
    """SuperAdmin-only master switch panel: which app capabilities are
    visible to the Admin role (ADMIN_NAV_FEATURES) and which chat/Settings
    features are visible to User/Manager/Admin (USER_CHAT_FEATURES) - see
    governance/models.py for the registries and RoleFeatureToggle, and
    governance/features.py for the enforcement helper every gated
    view/template actually calls. Independent of the per-Plan
    KNOWN_FEATURE_FLAGS - this is a role-wide switch, not a subscription
    grant."""

    template_name = "governance/feature_visibility.html"

    def get_context_data(self, **kwargs):
        existing = {(t.role, t.feature_key): t.is_enabled for t in RoleFeatureToggle.objects.all()}
        admin_rows = [
            {"key": key, "label": label, "enabled": existing.get(("admin", key), True)}
            for key, label in ADMIN_NAV_FEATURES
        ]
        user_rows = [
            {
                "key": key,
                "label": label,
                "enabled_by_role": {role: existing.get((role, key), True) for role in ROLE_FEATURE_ROLES},
            }
            for key, label in USER_CHAT_FEATURES
        ]
        return super().get_context_data(**kwargs) | {
            "admin_rows": admin_rows,
            "user_rows": user_rows,
            "roles": ROLE_FEATURE_ROLES,
        }

    def post(self, request):
        changed = []
        with transaction.atomic():
            for key, _label in ADMIN_NAV_FEATURES:
                enabled = request.POST.get(f"toggle_{key}_admin") == "on"
                toggle, _created = RoleFeatureToggle.objects.update_or_create(
                    role="admin", feature_key=key, defaults={"is_enabled": enabled}
                )
                changed.append(f"admin:{key}={enabled}")
            for key, _label in USER_CHAT_FEATURES:
                for role in ROLE_FEATURE_ROLES:
                    enabled = request.POST.get(f"toggle_{key}_{role}") == "on"
                    RoleFeatureToggle.objects.update_or_create(
                        role=role, feature_key=key, defaults={"is_enabled": enabled}
                    )
                    changed.append(f"{role}:{key}={enabled}")

        log_action(request.user, "feature_visibility.update", request.user, new_value=", ".join(changed)[:2000])
        django_messages.success(request, _("Feature visibility updated."))
        return redirect("governance:feature_visibility")
