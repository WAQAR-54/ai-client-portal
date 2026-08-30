from decimal import Decimal, InvalidOperation

from django.contrib import messages as django_messages
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

from accounts.models import Department, User
from accounts.permissions import AdminRequiredMixin, role_required
from chat.models import Conversation, Message, MessageFeedback, ModelConfig, PromptTemplate, UserModelPermission
from governance.audit import log_action
from governance.limits import _effective_limit, _metric
from governance.models import AuditLog, KNOWN_FEATURE_FLAGS, Plan, SystemPromptVersion, UpgradeRequest, UsageLimit
from governance.plans import (
    assign_plan,
    clear_user_overrides,
    count_user_overrides,
    engagement_score,
    get_assignment,
    get_plan_status,
    get_user_overrides,
)


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


def _org_usage_overview():
    """Aggregate, read-only snapshot for the admin Overview page's usage
    ring: average pct-of-monthly-token-cap across users who have an
    effective limit with a monthly_token_cap set. Purely additive — reuses
    _effective_limit()/_metric() from governance.limits but never touches
    check_usage_limits()/validate_upload()."""
    month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    pct_values = []
    over_80_count = 0
    for user in User.objects.all():
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


class DashboardView(AdminRequiredMixin, TemplateView):
    template_name = "governance/dashboard.html"

    def get_context_data(self, **kwargs):
        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        assistant_messages = Message.objects.filter(role=Message.Role.ASSISTANT)
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

        by_model = (
            assistant_messages.exclude(model_used__isnull=True)
            .values("model_used__model_name")
            .annotate(cost=Sum("estimated_cost"))
            .order_by("-cost")[:8]
        )
        model_labels = [row["model_used__model_name"] for row in by_model]
        model_cost = [float(row["cost"] or 0) for row in by_model]

        role_counts = User.objects.values("role").annotate(count=Count("id")).order_by("role")
        role_labels = [dict(User.Role.choices).get(row["role"], row["role"]) for row in role_counts]
        role_values = [row["count"] for row in role_counts]

        return super().get_context_data(**kwargs) | {
            "total_users": User.objects.count(),
            "total_conversations": Conversation.objects.count(),
            "total_tokens_all_time": assistant_messages.aggregate(total=Sum(F("input_tokens") + F("output_tokens")))[
                "total"
            ]
            or 0,
            "total_cost_all_time": assistant_messages.aggregate(total=Sum("estimated_cost"))["total"] or 0,
            "month_tokens": month_messages.aggregate(total=Sum(F("input_tokens") + F("output_tokens")))["total"] or 0,
            "month_cost": month_messages.aggregate(total=Sum("estimated_cost"))["total"] or 0,
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
            "org_usage": _org_usage_overview(),
        }


class UserListView(FilterableListMixin, AdminRequiredMixin, ListView):
    model = User
    template_name = "governance/users.html"
    partial_template_name = "governance/_users_table.html"
    context_object_name = "users"

    def get_queryset(self):
        qs = User.objects.select_related("department", "plan_assignment__plan").order_by("email")
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
        plan_info = {}
        for u in users:
            status = get_plan_status(u)
            plan_info[u.id] = {
                **status,
                "engagement": engagement_score(u),
                "override_count": count_user_overrides(u),
            }

        if self.request.GET.get("sort") == "engagement":
            users.sort(key=lambda u: plan_info[u.id]["engagement"] or -1, reverse=True)
            ctx[self.context_object_name] = users

        return ctx | {
            "roles": User.Role.choices,
            "departments": Department.objects.order_by("name"),
            "plans": Plan.objects.filter(is_active=True, is_visible_to_admins=True).order_by("name"),
            "plan_info": plan_info,
            "search": self.request.GET.get("search", ""),
            "role_filter": self.request.GET.get("role", ""),
            "status_filter": self.request.GET.get("status", ""),
            "plan_filter": self.request.GET.get("plan", ""),
            "sort": self.request.GET.get("sort", ""),
            "total_count": User.objects.count(),
        }


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def toggle_user_active(request, user_id):
    target = get_object_or_404(User, id=user_id)
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
def change_user_role(request, user_id):
    target = get_object_or_404(User, id=user_id)
    new_role = request.POST.get("role")
    if new_role not in User.Role.values:
        return HttpResponseBadRequest("Invalid role")
    old_value = target.role
    target.role = new_role
    target.save(update_fields=["role"])
    log_action(request.user, "user.role_change", target, old_value=old_value, new_value=new_role)
    _notify_admin_change(target, f"Your role was changed to {target.get_role_display()}.")
    return redirect("governance:users")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def change_user_department(request, user_id):
    target = get_object_or_404(User, id=user_id)
    department_id = request.POST.get("department_id") or None
    old_value = target.department_id
    target.department_id = department_id
    target.save(update_fields=["department"])
    log_action(request.user, "user.department_change", target, old_value=old_value, new_value=department_id)
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
    target = get_object_or_404(User, id=user_id)
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
    for target in User.objects.filter(id__in=user_ids):
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
        target = get_object_or_404(User, id=kwargs["user_id"])
        return super().get_context_data(**kwargs) | {
            "target": target,
            "overrides": get_user_overrides(target),
            "plan_status": get_plan_status(target),
        }


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def clear_user_overrides_view(request, user_id):
    target = get_object_or_404(User, id=user_id)
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


class PlanListView(AdminRequiredMixin, ListView):
    model = Plan
    template_name = "governance/plans.html"
    context_object_name = "plans"
    queryset = Plan.objects.order_by("-is_default", "name")


class PlanFormView(AdminRequiredMixin, TemplateView):
    template_name = "governance/plan_form.html"

    def get_context_data(self, **kwargs):
        plan = get_object_or_404(Plan, id=kwargs["plan_id"]) if kwargs.get("plan_id") else None
        existing_flags = plan.feature_flags if plan else {}
        return super().get_context_data(**kwargs) | {
            "plan": plan,
            "models": ModelConfig.objects.order_by("provider", "model_name"),
            "selected_model_ids": set(plan.allowed_models.values_list("id", flat=True)) if plan else set(),
            "known_flags": [(key, label, bool(existing_flags.get(key))) for key, label in KNOWN_FEATURE_FLAGS],
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
        return redirect("governance:plans")


class UpgradeRequestListView(AdminRequiredMixin, ListView):
    model = UpgradeRequest
    template_name = "governance/upgrade_requests.html"
    context_object_name = "upgrade_requests"
    queryset = UpgradeRequest.objects.select_related("user", "current_plan").filter(
        status=UpgradeRequest.Status.PENDING,
    )


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def resolve_upgrade_request(request, request_id):
    upgrade_request = get_object_or_404(UpgradeRequest, id=request_id)
    action = request.POST.get("action")

    upgrade_request.resolved_at = timezone.now()
    upgrade_request.resolved_by = request.user

    if action == "approve":
        upgrade_request.status = UpgradeRequest.Status.APPROVED
        upgrade_request.save()
        log_action(request.user, "upgrade_request.approve", upgrade_request)
        return redirect(f"{reverse('governance:users')}?search={upgrade_request.user.email}")

    upgrade_request.status = UpgradeRequest.Status.DISMISSED
    upgrade_request.save()
    log_action(request.user, "upgrade_request.dismiss", upgrade_request)
    return redirect("governance:upgrade_requests")


class ModelListView(FilterableListMixin, AdminRequiredMixin, ListView):
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
            "providers": ModelConfig.Provider.choices,
            "tiers": ModelConfig.Tier.choices,
            "search": self.request.GET.get("search", ""),
            "status_filter": self.request.GET.get("status", ""),
            "total_count": ModelConfig.objects.count(),
        }


@role_required(User.Role.ADMIN)
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


@role_required(User.Role.ADMIN)
@require_GET
def sync_models_preview(request):
    from chat.model_sync import fetch_all_available_models, known_model_keys

    fetched = fetch_all_available_models()
    existing = known_model_keys()
    for provider, entry in fetched.items():
        entry["new_models"] = [m for m in entry["models"] if (provider, m) not in existing]
        entry["already_tracked"] = [m for m in entry["models"] if (provider, m) in existing]
    return render(request, "governance/model_sync.html", {"fetched": fetched, "tiers": ModelConfig.Tier.choices})


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def sync_models_import(request):
    created_count = 0
    for encoded in request.POST.getlist("model"):
        if "::" not in encoded:
            continue
        provider, model_name = encoded.split("::", 1)
        if provider not in ModelConfig.Provider.values:
            continue
        tier = request.POST.get(f"tier__{encoded}", ModelConfig.Tier.DEFAULT)
        if tier not in ModelConfig.Tier.values:
            tier = ModelConfig.Tier.DEFAULT
        model_config, was_created = ModelConfig.objects.get_or_create(
            provider=provider,
            model_name=model_name,
            defaults={"tier": tier},
        )
        if was_created:
            created_count += 1
            log_action(request.user, "model.sync_import", model_config, new_value=model_name)

    if created_count:
        django_messages.success(
            request,
            ngettext(
                "Imported %(count)s new model, disabled by default. "
                "Set pricing and enable it below before it's usable.",
                "Imported %(count)s new models, disabled by default. "
                "Set pricing and enable them below before they're usable.",
                created_count,
            )
            % {"count": created_count},
        )
    else:
        django_messages.info(request, _("No models were selected to import."))
    return redirect("governance:models")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def add_model(request):
    provider = request.POST.get("provider", "")
    model_name = request.POST.get("model_name", "").strip()
    tier = request.POST.get("tier", ModelConfig.Tier.DEFAULT)

    if provider not in ModelConfig.Provider.values or not model_name or tier not in ModelConfig.Tier.values:
        return HttpResponseBadRequest("Invalid model")

    model_config, created = ModelConfig.objects.get_or_create(
        provider=provider,
        model_name=model_name,
        defaults={"tier": tier},
    )
    if created:
        log_action(request.user, "model.add", model_config, new_value=model_name)
    return redirect("governance:models")


@role_required(User.Role.ADMIN)
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


class ModelPermissionsView(AdminRequiredMixin, TemplateView):
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
        permission, _ = UserModelPermission.objects.get_or_create(
            user=target,
            model_config=model_config,
            defaults={"is_allowed": True},
        )
        old_value = permission.is_allowed
        permission.is_allowed = not permission.is_allowed
        permission.save(update_fields=["is_allowed"])
        log_action(
            request.user,
            "model.permission_change",
            permission,
            old_value=old_value,
            new_value=permission.is_allowed,
        )
        return redirect("governance:model_permissions", model_id=model_config.id)


def _filtered_assistant_messages(request):
    """Shared by the Usage & Cost screen and its CSV/XLSX exports, so
    "export respects whatever filter is active" is true by construction -
    both read the exact same query, not two independently-maintained ones."""
    assistant_messages = Message.objects.filter(role=Message.Role.ASSISTANT)

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


class UsageSummaryView(FilterableListMixin, AdminRequiredMixin, TemplateView):
    template_name = "governance/usage.html"
    partial_template_name = "governance/_usage_table.html"

    def get_context_data(self, **kwargs):
        assistant_messages, filters = _filtered_assistant_messages(self.request)

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
        }


@role_required(User.Role.ADMIN)
@require_GET
def export_usage_csv(request):
    import csv

    assistant_messages, _ = _filtered_assistant_messages(request)
    rows = assistant_messages.select_related("conversation__user__department", "model_used").order_by("created_at")

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
                m.model_used.model_name if m.model_used else "",
                m.input_tokens or 0,
                m.output_tokens or 0,
                f"{m.estimated_cost or 0:.6f}",
            ]
        )
    return response


@role_required(User.Role.ADMIN)
@require_GET
def export_usage_xlsx(request):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    assistant_messages, _ = _filtered_assistant_messages(request)
    rows = assistant_messages.select_related("conversation__user__department", "model_used").order_by("created_at")

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
                m.model_used.model_name if m.model_used else "",
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


@role_required(User.Role.ADMIN)
@require_GET
def export_usage_monthly_summary(request):
    """Aggregates by department for one calendar month - suitable for
    internal billing/chargeback, per spec. `month` is "YYYY-MM"; defaults
    to the current month."""
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


class AuditLogListView(FilterableListMixin, AdminRequiredMixin, ListView):
    model = AuditLog
    template_name = "governance/audit_logs.html"
    partial_template_name = "governance/_audit_logs_table.html"
    context_object_name = "logs"
    paginate_by = 50

    def get_queryset(self):
        qs = AuditLog.objects.select_related("actor")
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
            "total_count": AuditLog.objects.count(),
            "querystring_without_page": _querystring_without(self.request, "page"),
        }


class FeedbackListView(FilterableListMixin, AdminRequiredMixin, ListView):
    """Response feedback (thumbs up/down) — visibility only, per spec: this
    never feeds back into model selection/routing automatically."""

    model = MessageFeedback
    template_name = "governance/feedback.html"
    partial_template_name = "governance/_feedback_table.html"
    context_object_name = "feedback_entries"
    paginate_by = 50

    def get_queryset(self):
        qs = MessageFeedback.objects.select_related("user", "model_used", "message", "message__conversation")
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
            "total_count": MessageFeedback.objects.count(),
            "querystring_without_page": _querystring_without(self.request, "page"),
        }


class DepartmentListView(FilterableListMixin, AdminRequiredMixin, ListView):
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


@role_required(User.Role.ADMIN)
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


@role_required(User.Role.ADMIN)
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


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def delete_department(request, department_id):
    department = get_object_or_404(Department, id=department_id)
    log_action(request.user, "department.delete", department, old_value=department.name)
    department.delete()
    return redirect("governance:departments")


class SystemPromptView(AdminRequiredMixin, TemplateView):
    template_name = "governance/system_prompt.html"

    def get_context_data(self, **kwargs):
        department = get_object_or_404(Department, id=kwargs["department_id"])
        return super().get_context_data(**kwargs) | {
            "department": department,
            "active_version": SystemPromptVersion.objects.filter(department=department, is_active=True).first(),
            "history": SystemPromptVersion.objects.filter(department=department).order_by("-created_at")[:10],
        }

    def post(self, request, department_id):
        department = get_object_or_404(Department, id=department_id)
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


class DepartmentTemplatesView(AdminRequiredMixin, TemplateView):
    """Team prompt templates for one department - visible to every user in
    that department alongside their own personal ones (see
    chat/views.py::_visible_prompt_templates)."""

    template_name = "governance/department_templates.html"

    def get_context_data(self, **kwargs):
        department = get_object_or_404(Department, id=kwargs["department_id"])
        return super().get_context_data(**kwargs) | {
            "department": department,
            "templates": PromptTemplate.objects.filter(department=department),
        }

    def post(self, request, department_id):
        department = get_object_or_404(Department, id=department_id)
        name = request.POST.get("name", "").strip()
        content = request.POST.get("content", "").strip()
        if name and content:
            template = PromptTemplate.objects.create(department=department, name=name[:100], content=content)
            log_action(request.user, "prompt_template.create", template, new_value=name)
        return redirect("governance:department_templates", department_id=department.id)


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def delete_department_template(request, department_id, template_id):
    template = get_object_or_404(PromptTemplate, id=template_id, department_id=department_id)
    log_action(request.user, "prompt_template.delete", template, old_value=template.name)
    template.delete()
    return redirect("governance:department_templates", department_id=department_id)


class LimitListView(FilterableListMixin, AdminRequiredMixin, ListView):
    model = UsageLimit
    template_name = "governance/limits.html"
    partial_template_name = "governance/_limits_table.html"
    context_object_name = "limits"

    def get_queryset(self):
        qs = UsageLimit.objects.select_related("user", "department")
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
            "total_count": UsageLimit.objects.count(),
        }


class LimitFormView(AdminRequiredMixin, TemplateView):
    template_name = "governance/limit_form.html"

    def get_context_data(self, **kwargs):
        limit = None
        if kwargs.get("limit_id"):
            limit = get_object_or_404(UsageLimit, id=kwargs["limit_id"])
        return super().get_context_data(**kwargs) | {
            "limit": limit,
            "users": User.objects.order_by("email"),
            "departments": Department.objects.order_by("name"),
        }

    def post(self, request, limit_id=None):
        limit = get_object_or_404(UsageLimit, id=limit_id) if limit_id else UsageLimit()

        target_type = request.POST.get("target_type")
        if target_type == "user":
            limit.user_id = request.POST.get("user_id") or None
            limit.department_id = None
        else:
            limit.department_id = request.POST.get("department_id") or None
            limit.user_id = None

        if not limit.user_id and not limit.department_id:
            return HttpResponseBadRequest("Pick a user or a department")

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
@require_http_methods(["POST"])
def delete_limit(request, limit_id):
    limit = get_object_or_404(UsageLimit, id=limit_id)
    log_action(request.user, "limit.delete", limit, old_value=str(limit))
    limit.delete()
    return redirect("governance:limits")
