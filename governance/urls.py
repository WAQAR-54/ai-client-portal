from django.urls import path

from governance import views

app_name = "governance"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("users/", views.UserListView.as_view(), name="users"),
    path("users/<int:user_id>/toggle-active/", views.toggle_user_active, name="toggle_user_active"),
    path("users/<int:user_id>/change-role/", views.change_user_role, name="change_user_role"),
    path("users/<int:user_id>/change-department/", views.change_user_department, name="change_user_department"),
    path("users/<int:user_id>/change-plan/", views.change_user_plan, name="change_user_plan"),
    path("users/bulk-change-plan/", views.bulk_change_plan, name="bulk_change_plan"),
    path("users/<int:user_id>/overrides/", views.UserOverridesView.as_view(), name="user_overrides"),
    path("users/<int:user_id>/overrides/clear/", views.clear_user_overrides_view, name="clear_user_overrides"),
    path("plans/", views.PlanListView.as_view(), name="plans"),
    path("plans/new/", views.PlanFormView.as_view(), name="plan_new"),
    path("plans/<int:plan_id>/edit/", views.PlanFormView.as_view(), name="plan_edit"),
    path("upgrade-requests/", views.UpgradeRequestListView.as_view(), name="upgrade_requests"),
    path("upgrade-requests/<int:request_id>/resolve/", views.resolve_upgrade_request, name="resolve_upgrade_request"),
    path("models/", views.ModelListView.as_view(), name="models"),
    path("models/sync/", views.sync_models_preview, name="sync_models_preview"),
    path("models/sync/import/", views.sync_models_import, name="sync_models_import"),
    path("models/add/", views.add_model, name="add_model"),
    path("models/<int:model_id>/toggle-enabled/", views.toggle_model_enabled, name="toggle_model_enabled"),
    path("models/<int:model_id>/pricing/", views.update_model_pricing, name="update_model_pricing"),
    path("models/<int:model_id>/permissions/", views.ModelPermissionsView.as_view(), name="model_permissions"),
    path("usage/", views.UsageSummaryView.as_view(), name="usage"),
    path("usage/export.csv", views.export_usage_csv, name="export_usage_csv"),
    path("usage/export.xlsx", views.export_usage_xlsx, name="export_usage_xlsx"),
    path("usage/export-monthly-summary.xlsx", views.export_usage_monthly_summary, name="export_usage_monthly_summary"),
    path("limits/", views.LimitListView.as_view(), name="limits"),
    path("limits/new/", views.LimitFormView.as_view(), name="limit_new"),
    path("limits/<int:limit_id>/edit/", views.LimitFormView.as_view(), name="limit_edit"),
    path("limits/<int:limit_id>/delete/", views.delete_limit, name="limit_delete"),
    path("audit-logs/", views.AuditLogListView.as_view(), name="audit_logs"),
    path("feedback/", views.FeedbackListView.as_view(), name="feedback"),
    path("departments/", views.DepartmentListView.as_view(), name="departments"),
    path("departments/add/", views.add_department, name="add_department"),
    path("departments/<int:department_id>/update/", views.update_department, name="update_department"),
    path("departments/<int:department_id>/delete/", views.delete_department, name="delete_department"),
    path("departments/<int:department_id>/system-prompt/", views.SystemPromptView.as_view(), name="system_prompt"),
    path(
        "departments/<int:department_id>/templates/",
        views.DepartmentTemplatesView.as_view(),
        name="department_templates",
    ),
    path(
        "departments/<int:department_id>/templates/<int:template_id>/delete/",
        views.delete_department_template,
        name="delete_department_template",
    ),
]
