from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import RedirectView, TemplateView

from accounts.forms import EmailAuthenticationForm, ProfileForm, SignupForm
from accounts.permissions import AdminRequiredMixin


class PortalLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True

    def get_success_url(self):
        return reverse_lazy("accounts:dashboard")


def logout_view(request):
    logout(request)
    return redirect("accounts:login")


def signup_view(request):
    if request.user.is_authenticated:
        return redirect("accounts:dashboard")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect("accounts:dashboard")
    else:
        form = SignupForm()

    return render(request, "accounts/signup.html", {"form": form})


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["user"] = self.request.user
        return context


class AdminPanelView(AdminRequiredMixin, RedirectView):
    """Kept for backward-compatible URLs; the real admin dashboard lives in the governance app."""

    pattern_name = "governance:dashboard"


class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {
            "profile_form": ProfileForm(instance=self.request.user),
            "password_form": PasswordChangeForm(user=self.request.user),
        }

    def post(self, request):
        profile_form = ProfileForm(request.POST, instance=request.user)
        if profile_form.is_valid():
            profile_form.save()
            messages.success(request, "Profile updated.")
            return redirect("accounts:profile")
        return render(request, self.template_name, {
            "profile_form": profile_form, "password_form": PasswordChangeForm(user=request.user),
        })


class ProfilePasswordView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/profile.html"

    def post(self, request):
        password_form = PasswordChangeForm(user=request.user, data=request.POST)
        if password_form.is_valid():
            password_form.save()
            update_session_auth_hash(request, password_form.user)
            messages.success(request, "Password changed.")
            return redirect("accounts:profile")
        return render(request, self.template_name, {
            "profile_form": ProfileForm(instance=request.user), "password_form": password_form,
        })
