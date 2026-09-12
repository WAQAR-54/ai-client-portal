from django.urls import path

from billing import views

app_name = "billing"

urlpatterns = [
    path("pricing/", views.PublicPricingView.as_view(), name="public_pricing"),
    path("regional-pricing/", views.RegionalPricingView.as_view(), name="regional_pricing"),
    path(
        "regional-pricing/<int:plan_id>/update/",
        views.update_plan_regional_pricing,
        name="update_plan_regional_pricing",
    ),
    path("regional-pricing/add-region/", views.add_region, name="add_region"),
    path("organization-billing/", views.OrganizationBillingSettingsView.as_view(), name="organization_billing"),
    path(
        "organization-billing/update/",
        views.update_organization_billing_profile,
        name="update_organization_billing",
    ),
    path(
        "departments/<int:department_id>/billing-profile/",
        views.DepartmentBillingProfileView.as_view(),
        name="department_billing_profile",
    ),
    path(
        "departments/<int:department_id>/billing-profile/update/",
        views.update_department_billing_profile,
        name="update_department_billing_profile",
    ),
]
