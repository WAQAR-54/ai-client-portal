from django.urls import path

from governance import views

app_name = "governance"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("users/", views.UserListView.as_view(), name="users"),
    path("users/<int:user_id>/toggle-active/", views.toggle_user_active, name="toggle_user_active"),
    path("users/<int:user_id>/change-role/", views.change_user_role, name="change_user_role"),
    path("models/", views.ModelListView.as_view(), name="models"),
    path("models/<int:model_id>/toggle-enabled/", views.toggle_model_enabled, name="toggle_model_enabled"),
    path("usage/", views.UsageSummaryView.as_view(), name="usage"),
    path("audit-logs/", views.AuditLogListView.as_view(), name="audit_logs"),
    path("departments/", views.DepartmentListView.as_view(), name="departments"),
    path("departments/<int:department_id>/system-prompt/", views.SystemPromptView.as_view(), name="system_prompt"),
]
