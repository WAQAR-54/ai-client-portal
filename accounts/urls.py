from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("mfa/verify/", views.MFAVerifyView.as_view(), name="mfa_verify"),
    path("mfa/resend/", views.resend_mfa_code, name="resend_mfa_code"),
    path("profile/mfa/toggle/", views.toggle_own_mfa, name="toggle_own_mfa"),
    path("signup/", views.signup_view, name="signup"),
    path("password-reset/", views.password_reset_request_view, name="password_reset_request"),
    path(
        "password-reset/confirm/<uidb64>/<token>/",
        views.password_reset_confirm_view,
        name="password_reset_confirm",
    ),
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    path("admin-panel/", views.AdminPanelView.as_view(), name="admin_panel"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("profile/password/", views.ProfilePasswordView.as_view(), name="profile_password"),
    path("set-language/", views.set_language_preference, name="set_language_preference"),
    path("set-theme/", views.set_theme_preference, name="set_theme_preference"),
    path("onboarding/complete/", views.complete_onboarding, name="complete_onboarding"),
    path("onboarding/replay/", views.replay_onboarding, name="replay_onboarding"),
]
