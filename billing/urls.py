from django.urls import path

from billing import views

app_name = "billing"

urlpatterns = [
    path("regional-pricing/", views.RegionalPricingView.as_view(), name="regional_pricing"),
    path(
        "regional-pricing/<int:plan_id>/update/",
        views.update_plan_regional_pricing,
        name="update_plan_regional_pricing",
    ),
    path("regional-pricing/add-region/", views.add_region, name="add_region"),
]
