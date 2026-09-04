from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("chat/", include("chat.urls")),
    path("governance/", include("governance.urls")),
    path("providers/", include("providers.urls")),
    path("notifications/", include("notifications.urls")),
    path("", RedirectView.as_view(pattern_name="accounts:dashboard", permanent=False)),
]
