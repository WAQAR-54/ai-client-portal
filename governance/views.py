from decimal import Decimal, InvalidOperation

from django.db.models import Count, F, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.generic import ListView, TemplateView

from accounts.models import Department, User
from accounts.permissions import AdminRequiredMixin, role_required
from chat.models import Conversation, Message, ModelConfig, UserModelPermission
from governance.audit import log_action
from governance.models import AuditLog, SystemPromptVersion, UsageLimit


class DashboardView(AdminRequiredMixin, TemplateView):
    template_name = "governance/dashboard.html"

    def get_context_data(self, **kwargs):
        import json

        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        assistant_messages = Message.objects.filter(role=Message.Role.ASSISTANT)
        month_messages = assistant_messages.filter(created_at__gte=month_start)

        fourteen_days_ago = timezone.now() - timezone.timedelta(days=14)
        daily = (
            assistant_messages.filter(created_at__gte=fourteen_days_ago)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(tokens=Sum(F("input_tokens") + F("output_tokens")), cost=Sum("estimated_cost"))
            .order_by("day")
        )
        daily_labels = [row["day"].strftime("%b %d") for row in daily]
        daily_tokens = [row["tokens"] or 0 for row in daily]
        daily_cost = [float(row["cost"] or 0) for row in daily]

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
            "total_tokens_all_time": assistant_messages.aggregate(
                total=Sum(F("input_tokens") + F("output_tokens")))["total"] or 0,
            "total_cost_all_time": assistant_messages.aggregate(total=Sum("estimated_cost"))["total"] or 0,
            "month_tokens": month_messages.aggregate(
                total=Sum(F("input_tokens") + F("output_tokens")))["total"] or 0,
            "month_cost": month_messages.aggregate(total=Sum("estimated_cost"))["total"] or 0,
            "enabled_model_count": ModelConfig.objects.filter(is_enabled=True).count(),
            "chart_daily_labels": json.dumps(daily_labels),
            "chart_daily_tokens": json.dumps(daily_tokens),
            "chart_daily_cost": json.dumps(daily_cost),
            "chart_model_labels": json.dumps(model_labels),
            "chart_model_cost": json.dumps(model_cost),
            "chart_role_labels": json.dumps(role_labels),
            "chart_role_values": json.dumps(role_values),
        }


class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = "governance/users.html"
    context_object_name = "users"
    queryset = User.objects.select_related("department").order_by("email")

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {
            "roles": User.Role.choices, "departments": Department.objects.order_by("name"),
        }


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def toggle_user_active(request, user_id):
    target = get_object_or_404(User, id=user_id)
    old_value = target.is_active
    target.is_active = not target.is_active
    target.save(update_fields=["is_active"])
    log_action(request.user, "user.suspend" if not target.is_active else "user.activate", target,
               old_value=old_value, new_value=target.is_active)
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


class ModelListView(AdminRequiredMixin, ListView):
    model = ModelConfig
    template_name = "governance/models.html"
    context_object_name = "models"

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {
            "providers": ModelConfig.Provider.choices, "tiers": ModelConfig.Tier.choices,
        }


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def toggle_model_enabled(request, model_id):
    model_config = get_object_or_404(ModelConfig, id=model_id)
    old_value = model_config.is_enabled
    model_config.is_enabled = not model_config.is_enabled
    model_config.save(update_fields=["is_enabled"])
    log_action(request.user, "model.enable" if model_config.is_enabled else "model.disable", model_config,
               old_value=old_value, new_value=model_config.is_enabled)
    return redirect("governance:models")


def _parse_decimal(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def add_model(request):
    provider = request.POST.get("provider", "")
    model_name = request.POST.get("model_name", "").strip()
    tier = request.POST.get("tier", ModelConfig.Tier.DEFAULT)

    if provider not in ModelConfig.Provider.values or not model_name or tier not in ModelConfig.Tier.values:
        return HttpResponseBadRequest("Invalid model")

    model_config, created = ModelConfig.objects.get_or_create(
        provider=provider, model_name=model_name, defaults={"tier": tier},
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
    model_config.save(update_fields=["input_cost_per_1m", "output_cost_per_1m"])
    log_action(
        request.user, "model.pricing_update", model_config, old_value=old_value,
        new_value=f"in={model_config.input_cost_per_1m} out={model_config.output_cost_per_1m}",
    )
    return redirect("governance:models")


class ModelPermissionsView(AdminRequiredMixin, TemplateView):
    template_name = "governance/model_permissions.html"

    def get_context_data(self, **kwargs):
        model_config = get_object_or_404(ModelConfig, id=kwargs["model_id"])
        denied_user_ids = set(
            UserModelPermission.objects.filter(model_config=model_config, is_allowed=False)
            .values_list("user_id", flat=True)
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
            user=target, model_config=model_config, defaults={"is_allowed": True},
        )
        old_value = permission.is_allowed
        permission.is_allowed = not permission.is_allowed
        permission.save(update_fields=["is_allowed"])
        log_action(
            request.user, "model.permission_change", permission,
            old_value=old_value, new_value=permission.is_allowed,
        )
        return redirect("governance:model_permissions", model_id=model_config.id)


class UsageSummaryView(AdminRequiredMixin, TemplateView):
    template_name = "governance/usage.html"

    def get_context_data(self, **kwargs):
        per_user = (
            Message.objects.filter(role=Message.Role.ASSISTANT)
            .values("conversation__user__email")
            .annotate(
                requests=Count("id"),
                tokens=Sum(F("input_tokens") + F("output_tokens")),
                cost=Sum("estimated_cost"),
            )
            .order_by("-cost")
        )
        return super().get_context_data(**kwargs) | {"per_user": per_user}


class AuditLogListView(AdminRequiredMixin, ListView):
    model = AuditLog
    template_name = "governance/audit_logs.html"
    context_object_name = "logs"
    paginate_by = 50
    queryset = AuditLog.objects.select_related("actor")


class DepartmentListView(AdminRequiredMixin, ListView):
    model = Department
    template_name = "governance/departments.html"
    context_object_name = "departments"


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def add_department(request):
    name = request.POST.get("name", "").strip()
    if not name:
        return HttpResponseBadRequest("Name is required")
    budget_cap = _parse_decimal(request.POST.get("monthly_budget_cap"))
    department, created = Department.objects.get_or_create(
        name=name, defaults={"monthly_budget_cap": budget_cap},
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
        request.user, "department.update", department, old_value=old_value,
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
            department=department, content=content, tone_preference=tone_preference,
            restricted_topics=restricted_topics, created_by=request.user,
        )
        log_action(request.user, "system_prompt.new_version", new_version,
                   old_value="", new_value=content[:200])
        return redirect("governance:system_prompt", department_id=department.id)


class LimitListView(AdminRequiredMixin, ListView):
    model = UsageLimit
    template_name = "governance/limits.html"
    context_object_name = "limits"
    queryset = UsageLimit.objects.select_related("user", "department")

    def get_context_data(self, **kwargs):
        from django.conf import settings
        return super().get_context_data(**kwargs) | {
            "default_upload_mb": settings.DEFAULT_MAX_UPLOAD_SIZE_MB,
            "default_extensions": settings.DEFAULT_ALLOWED_FILE_EXTENSIONS,
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
            request.user, "limit.create" if is_new else "limit.update", limit,
            new_value=f"user={limit.user_id} dept={limit.department_id}",
        )
        return redirect("governance:limits")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def delete_limit(request, limit_id):
    limit = get_object_or_404(UsageLimit, id=limit_id)
    log_action(request.user, "limit.delete", limit, old_value=str(limit))
    limit.delete()
    return redirect("governance:limits")
