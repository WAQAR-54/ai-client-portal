from django.urls import path

from providers import views

app_name = "providers"

urlpatterns = [
    path("", views.ProviderListView.as_view(), name="list"),
    path("<int:provider_id>/connect/", views.connect_provider, name="connect"),
    path("<int:provider_id>/approve/", views.approve_provider, name="approve"),
    path("<int:provider_id>/reject/", views.reject_provider, name="reject"),
    path("<int:provider_id>/resync/", views.resync_provider, name="resync"),
    path("<int:provider_id>/disconnect/", views.disconnect_provider, name="disconnect"),
    path("<int:provider_id>/region/", views.update_provider_region, name="update_region"),
    path("models/<int:model_id>/toggle/", views.toggle_provider_model, name="toggle_model"),
    path(
        "models/<int:model_id>/toggle-manager-assignable/",
        views.toggle_provider_model_manager_assignable,
        name="toggle_model_manager_assignable",
    ),
    path("models/<int:model_id>/toggle-vision/", views.toggle_provider_model_vision, name="toggle_model_vision"),
]
