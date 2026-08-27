from django.db.models import Count, F, Sum
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.generic import ListView, TemplateView

from accounts.models import Department, User
from accounts.permissions import AdminRequiredMixin, role_required
from chat.models import Conversation, Message, ModelConfig
from governance.audit import log_action
from governance.models import AuditLog, SystemPromptVersion


class DashboardView(AdminRequiredMixin, TemplateView):
    template_name = "governance/dashboard.html"

    def get_context_data(self, **kwargs):
        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        assistant_messages = Message.objects.filter(role=Message.Role.ASSISTANT)
        month_messages = assistant_messages.filter(created_at__gte=month_start)

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
        }


class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = "governance/users.html"
    context_object_name = "users"
    queryset = User.objects.select_related("department").order_by("email")

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {"roles": User.Role.choices}


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


class ModelListView(AdminRequiredMixin, ListView):
    model = ModelConfig
    template_name = "governance/models.html"
    context_object_name = "models"


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
