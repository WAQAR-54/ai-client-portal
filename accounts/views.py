from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import translation
from django.views.decorators.http import require_POST
from django.views.generic import RedirectView, TemplateView

from accounts.forms import EmailAuthenticationForm, ProfileForm, SignupForm
from accounts.permissions import AdminRequiredMixin
from governance.features import require_feature


@login_required
@require_POST
def set_language_preference(request):
    """Settings -> language toggle. Persists to the user's own record (see
    accounts/middleware.py) rather than only the session, so the choice
    survives to the next login/device, not just this browser session."""
    language = request.POST.get("language", "").strip()
    valid_codes = {code for code, _ in settings.LANGUAGES}
    if language in valid_codes:
        request.user.preferred_language = language
        request.user.save(update_fields=["preferred_language"])
        translation.activate(language)
    return redirect(reverse("accounts:profile"))


@login_required
@require_feature("dark_mode")
@require_POST
def set_theme_preference(request):
    """Settings -> Display tab. Persisted to the user's own record (not just
    localStorage) so the choice follows them across devices/logins, per the
    dark-mode spec. Returns 204 (no redirect) since this is called via
    fetch() from the Settings page so the new theme applies instantly
    without a full page reload."""
    theme = request.POST.get("theme", "").strip()
    if theme in {"light", "dark", "system"}:
        request.user.theme_preference = theme
        request.user.save(update_fields=["theme_preference"])
    return HttpResponse(status=204)


@login_required
@require_POST
def complete_onboarding(request):
    """Marks the first-login guided tour seen (Next-through-the-end or
    Skip both call this - there's no meaningful difference in outcome)."""
    request.user.has_seen_onboarding = True
    request.user.save(update_fields=["has_seen_onboarding"])
    return HttpResponse(status=204)


@login_required
@require_feature("onboarding_tour")
@require_POST
def replay_onboarding(request):
    """Settings -> "Replay tour": resets the flag and sends the user back
    to chat, where the tour auto-starts again on load."""
    request.user.has_seen_onboarding = False
    request.user.save(update_fields=["has_seen_onboarding"])
    return redirect("chat:chat_home")


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
            # Carries over whatever language was already active for this
            # request (geo-guessed or explicitly chosen pre-signup - see
            # accounts/middleware.py) instead of resetting to the "en"
            # model default the moment they're logged in.
            active_language = translation.get_language()
            if active_language and active_language != user.preferred_language:
                user.preferred_language = active_language
                user.save(update_fields=["preferred_language"])
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
        from notifications.models import EMAIL_TOGGLE_LABELS, NotificationPreference

        preference, _ = NotificationPreference.objects.get_or_create(user=self.request.user)
        return super().get_context_data(**kwargs) | {
            "profile_form": ProfileForm(instance=self.request.user),
            "password_form": PasswordChangeForm(user=self.request.user),
            "notification_preference": preference,
            "notification_toggles": EMAIL_TOGGLE_LABELS,
        }

    def post(self, request):
        profile_form = ProfileForm(request.POST, instance=request.user)
        if profile_form.is_valid():
            profile_form.save()
            messages.success(request, translation.gettext("Profile updated."))
            return redirect("accounts:profile")
        return render(request, self.template_name, self.get_context_data() | {"profile_form": profile_form})


class ProfilePasswordView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/profile.html"

    def post(self, request):
        from notifications.models import EMAIL_TOGGLE_LABELS, NotificationPreference

        password_form = PasswordChangeForm(user=request.user, data=request.POST)
        if password_form.is_valid():
            password_form.save()
            update_session_auth_hash(request, password_form.user)
            messages.success(request, translation.gettext("Password changed."))
            return redirect("accounts:profile")
        preference, _ = NotificationPreference.objects.get_or_create(user=request.user)
        return render(
            request,
            self.template_name,
            {
                "profile_form": ProfileForm(instance=request.user),
                "password_form": password_form,
                "notification_preference": preference,
                "notification_toggles": EMAIL_TOGGLE_LABELS,
            },
        )
