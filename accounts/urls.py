from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("signup/", views.signup_view, name="signup"),
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    path("admin-panel/", views.AdminPanelView.as_view(), name="admin_panel"),
]
