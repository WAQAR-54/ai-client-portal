from django.urls import path

from governance import views

app_name = "governance"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("users/", views.UserListView.as_view(), name="users"),
    path("users/<int:user_id>/toggle-active/", views.toggle_user_active, name="toggle_user_active"),
    path("users/<int:user_id>/change-role/", views.change_user_role, name="change_user_role"),
    path("users/<int:user_id>/change-department/", views.change_user_department, name="change_user_department"),
    path("models/", views.ModelListView.as_view(), name="models"),
    path("models/add/", views.add_model, name="add_model"),
    path("models/<int:model_id>/toggle-enabled/", views.toggle_model_enabled, name="toggle_model_enabled"),
    path("models/<int:model_id>/pricing/", views.update_model_pricing, name="update_model_pricing"),
    path("models/<int:model_id>/permissions/", views.ModelPermissionsView.as_view(), name="model_permissions"),
    path("usage/", views.UsageSummaryView.as_view(), name="usage"),
    path("limits/", views.LimitListView.as_view(), name="limits"),
    path("limits/new/", views.LimitFormView.as_view(), name="limit_new"),
    path("limits/<int:limit_id>/edit/", views.LimitFormView.as_view(), name="limit_edit"),
    path("limits/<int:limit_id>/delete/", views.delete_limit, name="limit_delete"),
    path("audit-logs/", views.AuditLogListView.as_view(), name="audit_logs"),
    path("departments/", views.DepartmentListView.as_view(), name="departments"),
    path("departments/add/", views.add_department, name="add_department"),
    path("departments/<int:department_id>/update/", views.update_department, name="update_department"),
    path("departments/<int:department_id>/delete/", views.delete_department, name="delete_department"),
    path("departments/<int:department_id>/system-prompt/", views.SystemPromptView.as_view(), name="system_prompt"),
]
