from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import LoginView
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import translation
from django.utils.encoding import force_bytes
from django.utils.html import strip_tags
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.http import require_POST
from django.views.generic import RedirectView, TemplateView

from accounts.forms import (
    EmailAuthenticationForm,
    PortalPasswordResetForm,
    PortalSetPasswordForm,
    ProfileForm,
    SignupForm,
)
from accounts.mfa import (
    MAX_MFA_ATTEMPTS,
    OTP_EXPIRY_MINUTES,
    clear_mfa_session,
    mfa_challenge_expired,
    start_mfa_challenge,
    user_requires_mfa,
)
from accounts.models import User
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


def _send_mfa_code_email(request, user, code):
    """Sends the login verification code, through the same
    send_tracked_email() path (and EmailLog audit trail) every other real
    email in the app goes through."""
    from notifications.emailing import send_tracked_email

    with translation.override(user.preferred_language):
        html_body = render_to_string(
            "accounts/email_mfa_code.html", {"code": code, "expiry_minutes": OTP_EXPIRY_MINUTES}
        )
    send_tracked_email(
        to_email=user.email,
        subject="[AI Client Portal] Your verification code",
        text_body=strip_tags(html_body),
        html_body=html_body,
    )


class PortalLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True

    def get_success_url(self):
        # get_redirect_url() is LoginView's own safe "?next=" handling
        # (validates the URL against ALLOWED_HOSTS before ever using it, so
        # this can't be turned into an open redirect) - must be checked
        # first or every login-required deep link (Code Playground's
        # shared URL included) silently dumps the user on the generic
        # dashboard instead of back where they were headed.
        return self.get_redirect_url() or reverse_lazy("accounts:dashboard")

    def form_valid(self, form):
        """form_valid means the password already checked out (that's what
        AuthenticationForm.is_valid() verified) but form.get_user() is NOT
        yet actually logged in - super().form_valid() is what calls
        django.contrib.auth.login(). Intercepting here, before that call,
        is what lets MFA require a second step without ever granting a
        real session first."""
        user = form.get_user()
        if user_requires_mfa(user):
            code = start_mfa_challenge(self.request, user, str(self.get_success_url()))
            _send_mfa_code_email(self.request, user, code)
            return redirect("accounts:mfa_verify")
        return super().form_valid(form)


def logout_view(request):
    logout(request)
    return redirect("accounts:login")


def _mfa_redirect_target(request):
    """The safe (already-validated at storage time - see PortalLoginView.
    form_valid) URL to send the user to once MFA succeeds."""
    from django.urls import reverse

    return request.session.get("mfa_next") or reverse("accounts:dashboard")


class MFAVerifyView(TemplateView):
    """The second step of login for anyone user_requires_mfa() applies to -
    reached only via PortalLoginView.form_valid() redirecting here, never
    linked from anywhere else. dispatch() bounces back to the login form if
    there's no pending challenge (direct URL visit, expired-and-cleared
    session, or already completed) rather than showing a broken form."""

    template_name = "accounts/mfa_verify.html"

    def dispatch(self, request, *args, **kwargs):
        if "mfa_user_id" not in request.session:
            return redirect("accounts:login")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        if mfa_challenge_expired(request):
            clear_mfa_session(request)
            messages.error(request, translation.gettext("That code expired — log in again to get a new one."))
            return redirect("accounts:login")

        attempts = request.session.get("mfa_attempts", 0)
        if attempts >= MAX_MFA_ATTEMPTS:
            clear_mfa_session(request)
            messages.error(
                request, translation.gettext("Too many incorrect attempts — log in again to get a new code.")
            )
            return redirect("accounts:login")

        submitted = request.POST.get("code", "").strip()
        if submitted != request.session.get("mfa_code"):
            request.session["mfa_attempts"] = attempts + 1
            messages.error(request, translation.gettext("Incorrect code — please try again."))
            return self.get(request)

        user = User.objects.get(id=request.session["mfa_user_id"])
        next_url = _mfa_redirect_target(request)
        clear_mfa_session(request)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect(next_url)


@require_POST
def resend_mfa_code(request):
    if "mfa_user_id" not in request.session:
        return redirect("accounts:login")
    user = User.objects.get(id=request.session["mfa_user_id"])
    next_url = request.session.get("mfa_next", "")
    code = start_mfa_challenge(request, user, next_url)
    _send_mfa_code_email(request, user, code)
    messages.success(request, translation.gettext("A new code is on its way."))
    return redirect("accounts:mfa_verify")


@login_required
@require_POST
def toggle_own_mfa(request):
    """Self-service - User/Manager only in the UI (see profile.html), but
    the view itself doesn't need to enforce that: an Admin/SuperAdmin
    toggling their own mfa_enabled is harmless since user_requires_mfa()
    ignores that field for them entirely."""
    request.user.mfa_enabled = not request.user.mfa_enabled
    request.user.save(update_fields=["mfa_enabled"])
    messages.success(
        request,
        (
            translation.gettext("Two-step verification turned on.")
            if request.user.mfa_enabled
            else translation.gettext("Two-step verification turned off.")
        ),
    )
    return redirect("accounts:profile")


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


def _send_password_reset_email(request, user):
    """Sends the "reset your password" email for one user, through the same
    send_tracked_email() path (and EmailLog audit trail) every other real
    email in the app goes through - not Django's own PasswordResetForm.
    save(), which would bypass EmailLog and the admin-configurable
    EmailSettings entirely."""
    from notifications.emailing import send_tracked_email

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    reset_url = request.build_absolute_uri(
        reverse("accounts:password_reset_confirm", kwargs={"uidb64": uid, "token": token})
    )
    with translation.override(user.preferred_language):
        html_body = render_to_string("accounts/email_password_reset.html", {"user": user, "reset_url": reset_url})
    send_tracked_email(
        to_email=user.email,
        subject="[AI Client Portal] Reset your password",
        text_body=strip_tags(html_body),
        html_body=html_body,
    )


def password_reset_request_view(request):
    if request.user.is_authenticated:
        return redirect("accounts:dashboard")

    if request.method == "POST":
        form = PortalPasswordResetForm(request.POST)
        if form.is_valid():
            for user in form.get_users(form.cleaned_data["email"]):
                _send_password_reset_email(request, user)
            # Same message regardless of whether an account exists, so this
            # can't be used to check whether an email is registered.
            messages.success(
                request,
                translation.gettext("If an account exists for that email, we've sent a link to reset your password."),
            )
            return redirect("accounts:login")
    else:
        form = PortalPasswordResetForm()

    return render(request, "accounts/password_reset_request.html", {"form": form})


def password_reset_confirm_view(request, uidb64, token):
    if request.user.is_authenticated:
        return redirect("accounts:dashboard")

    try:
        user = User.objects.get(pk=urlsafe_base64_decode(uidb64).decode())
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    valid_link = user is not None and default_token_generator.check_token(user, token)
    if not valid_link:
        return render(request, "accounts/password_reset_confirm.html", {"valid_link": False})

    if request.method == "POST":
        form = PortalSetPasswordForm(user=user, data=request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, translation.gettext("Your password has been reset. You can log in now."))
            return redirect("accounts:login")
    else:
        form = PortalSetPasswordForm(user=user)

    return render(request, "accounts/password_reset_confirm.html", {"form": form, "valid_link": True})


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
