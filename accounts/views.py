from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import LoginView
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import translation
from django.utils.encoding import force_bytes
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
from accounts.google_auth import (
    GoogleSignInError,
    find_or_create_user_from_google,
    google_signin_enabled,
    verify_google_credential,
)
from accounts.mfa import (
    MAX_MFA_ATTEMPTS,
    MAX_MFA_RESENDS,
    clear_mfa_session,
    mfa_challenge_expired,
    start_mfa_challenge,
    user_requires_mfa,
)
from accounts.models import User
from accounts.permissions import AdminRequiredMixin
from accounts.rate_limit import (
    SECURITY_CRITICAL,
    client_ip,
    count_failure,
    is_over_limit,
    is_rate_limited,
)
from governance.features import require_feature

# Per-IP-per-hour caps on the two unauthenticated, abuse-prone endpoints
# django-axes doesn't cover (it only tracks LOGIN failures) - see
# accounts/rate_limit.py. Generous enough that a real person fumbling a
# signup form, or a shared office IP with several people resetting
# passwords the same afternoon, is never the one who hits these.
SIGNUP_RATE_LIMIT = 5
PASSWORD_RESET_IP_RATE_LIMIT = 10
PASSWORD_RESET_EMAIL_RATE_LIMIT = 3

# Per-username-per-hour cap on the login POST itself, counting EVERY
# submission (successful or not) - closes a gap axes leaves open. Axes only
# ever counts failed logins, so someone who already has valid (phished or
# leaked) credentials could otherwise resubmit the login form indefinitely
# to keep spawning brand-new MFA challenges - each with its own
# MAX_MFA_ATTEMPTS guesses and MAX_MFA_RESENDS resends (see accounts/mfa.py)
# - defeating that per-challenge cap by simply restarting the cycle instead
# of exhausting it. Keyed per-username (the dimension actually being
# abused), not per-IP, and generous enough that no real user's normal
# login/logout/typo pattern is ever the one who hits it.
LOGIN_RATE_LIMIT = 15

# Per-IP-per-hour cap on Google sign-in attempts. More generous than
# LOGIN_RATE_LIMIT/SIGNUP_RATE_LIMIT since one endpoint now covers both a
# first-time signup AND every later login for anyone using this button -
# each request still needs a fresh, Google-signed token (not a guessable
# credential), so this is defense in depth rather than the primary guard.
GOOGLE_SIGNIN_RATE_LIMIT = 20


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
    """Kicks off the login verification code email via Celery instead of
    blocking this request on an SMTP round-trip - see
    accounts/tasks.py::send_mfa_code_email_task."""
    from accounts.tasks import send_mfa_code_email_task

    send_mfa_code_email_task.delay(user.id, code)


def _begin_mfa_challenge_if_required(request, user, next_url):
    """Shared by password login (PortalLoginView.form_valid) and Google
    sign-in (google_signin below) - both need the exact same "start a
    fresh MFA challenge, or don't" branch once WHO is signing in is known
    but before django.contrib.auth.login() ever runs. Returns True if a
    challenge was started (the caller must send the user to mfa_verify
    instead of logging them in directly) - False means log them in now."""
    if not user_requires_mfa(user):
        return False
    code = start_mfa_challenge(request, user, next_url)
    # A genuinely fresh challenge - reset the resend cap here, not in
    # start_mfa_challenge itself (see its docstring).
    request.session["mfa_resend_count"] = 0
    _send_mfa_code_email(request, user, code)
    return True


class PortalLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {
            "google_signin_enabled": google_signin_enabled(),
            "google_client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        }

    def get_success_url(self):
        # get_redirect_url() is LoginView's own safe "?next=" handling
        # (validates the URL against ALLOWED_HOSTS before ever using it, so
        # this can't be turned into an open redirect) - must be checked
        # first or every login-required deep link (Code Playground's
        # shared URL included) silently dumps the user on the generic
        # dashboard instead of back where they were headed.
        return self.get_redirect_url() or reverse_lazy("accounts:dashboard")

    def post(self, request, *args, **kwargs):
        # Checked before super().post() even builds/validates the form -
        # see LOGIN_RATE_LIMIT's own comment for why this exists alongside
        # axes rather than being redundant with it.
        username = request.POST.get("username", "").strip().lower()
        # One source trying MANY usernames (credential stuffing) is invisible to the per-username
        # limits, so failures are also counted per IP. The answer is identical whether or not the
        # usernames exist, and only FAILURES count, so a shared office IP where everyone logs in
        # each morning is never the one that hits it.
        if is_over_limit(f"login_fail_ip:{client_ip(request)}", limit=settings.LOGIN_IP_FAILURE_LIMIT):
            messages.error(request, translation.gettext("Too many login attempts. Try again later."))
            return self.render_to_response(self.get_context_data(form=self.get_form_class()(request)))
        if username and is_rate_limited(
            f"login:{username}", limit=LOGIN_RATE_LIMIT, window_seconds=3600, policy=SECURITY_CRITICAL
        ):
            messages.error(request, translation.gettext("Too many login attempts for this account. Try again later."))
            return self.render_to_response(self.get_context_data(form=self.get_form_class()(request)))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        count_failure(f"login_fail_ip:{client_ip(self.request)}", window_seconds=3600)
        return super().form_invalid(form)

    def form_valid(self, form):
        """form_valid means the password already checked out (that's what
        AuthenticationForm.is_valid() verified) but form.get_user() is NOT
        yet actually logged in - super().form_valid() is what calls
        django.contrib.auth.login(). Intercepting here, before that call,
        is what lets MFA require a second step without ever granting a
        real session first."""
        user = form.get_user()
        if _begin_mfa_challenge_if_required(self.request, user, str(self.get_success_url())):
            return redirect("accounts:mfa_verify")
        return super().form_valid(form)


@require_POST
def google_signin(request):
    """POST target of the "Sign in with Google" button's JS callback (see
    templates/accounts/login.html and signup.html) - one endpoint serves
    both a first-time signup and every later login, since GIS's button
    doesn't distinguish the two and neither does Google's own concept of
    an ID token; find_or_create_user_from_google() is what decides which
    happened. Returns JSON rather than a redirect response because the
    caller is the button's own fetch(), not a form submission."""
    if not google_signin_enabled():
        return JsonResponse({"error": translation.gettext("Google sign-in isn't enabled.")}, status=403)
    if is_rate_limited(
        f"google_signin:{client_ip(request)}",
        limit=GOOGLE_SIGNIN_RATE_LIMIT,
        window_seconds=3600,
        policy=SECURITY_CRITICAL,
    ):
        return JsonResponse(
            {"error": translation.gettext("Too many attempts from this location. Try again later.")}, status=429
        )

    credential = request.POST.get("credential", "").strip()
    if not credential:
        return JsonResponse({"error": translation.gettext("Missing Google credential.")}, status=400)

    try:
        payload = verify_google_credential(credential)
    except GoogleSignInError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    user = find_or_create_user_from_google(payload)
    if not user.is_active:
        return JsonResponse({"error": translation.gettext("This account has been suspended.")}, status=403)

    next_url = str(reverse("accounts:dashboard"))
    if _begin_mfa_challenge_if_required(request, user, next_url):
        return JsonResponse({"redirect": str(reverse("accounts:mfa_verify"))})

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse({"redirect": next_url})


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

    resend_count = request.session.get("mfa_resend_count", 0)
    if resend_count >= MAX_MFA_RESENDS:
        # Forces a real re-login rather than an indefinite resend+retry
        # cycle - see MAX_MFA_RESENDS's own docstring in accounts/mfa.py.
        clear_mfa_session(request)
        messages.error(request, translation.gettext("Too many code requests — log in again to get a new code."))
        return redirect("accounts:login")
    request.session["mfa_resend_count"] = resend_count + 1

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

    google_context = {
        "google_signin_enabled": google_signin_enabled(),
        "google_client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
    }

    if request.method == "POST":
        # Unauthenticated by design (that's the point of self-service
        # signup) and django-axes only ever tracks LOGIN failures - so
        # this is the one thing standing between "one person signing up"
        # and automated mass account creation from a single IP.
        if is_rate_limited(
            f"signup:{client_ip(request)}", limit=SIGNUP_RATE_LIMIT, window_seconds=3600, policy=SECURITY_CRITICAL
        ):
            messages.error(
                request, translation.gettext("Too many signup attempts from this location. Try again later.")
            )
            return render(request, "accounts/signup.html", {"form": SignupForm()} | google_context)
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
            notify_self_signup_welcome(user)
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect("accounts:dashboard")
    else:
        form = SignupForm()

    return render(request, "accounts/signup.html", {"form": form} | google_context)


def notify_self_signup_welcome(user):
    """Welcome notice for someone who created their OWN account (password
    form here, or a first-time Google sign-in - see google_auth.py::
    find_or_create_user_from_google) - the self-signup counterpart to
    governance/views.py::_notify_account_created (admin-created accounts
    only; never both fire for the same user, since each call site only
    runs on its own creation path). Points at where to go next rather than
    login instructions, since self-signup already logs them in
    immediately - unlike an admin-created account, there's no separate
    password to relay."""
    from notifications.models import NotificationType
    from notifications.notify import notify

    with translation.override(user.preferred_language):
        title = translation.gettext("Welcome to AI Client Portal")
        body = translation.gettext(
            "Your account is ready. Head to Chat to start a conversation, or check My Plans to see what your "
            "plan includes."
        )
    notify(user, NotificationType.ACCOUNT_CREATED, title=title, body=body)


def _send_password_reset_email(request, user):
    """Kicks off the "reset your password" email via Celery instead of
    blocking this request on an SMTP round-trip - matches every other
    notification email in the app (notify() -> send_notification_email
    .delay()). uid/token are cheap to compute here (no network I/O); the
    actual send happens in accounts/tasks.py::send_password_reset_email_task,
    which builds the reset link from settings.SITE_URL since there's no
    request to call request.build_absolute_uri() on inside a Celery task."""
    from accounts.tasks import send_password_reset_email_task

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    send_password_reset_email_task.delay(user.id, uid, token)


def password_reset_request_view(request):
    if request.user.is_authenticated:
        return redirect("accounts:dashboard")

    if request.method == "POST":
        form = PortalPasswordResetForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"]
            # Rate-limited by IP (stop one source mass-requesting resets
            # across many addresses) AND by the target email itself (stop
            # that address's inbox being bombed with reset links from
            # many sources) - checked with `or` so either alone is
            # enough to suppress the send. Never surfaced to the caller:
            # the exact same generic message is shown either way, same
            # as the existing account-enumeration defense below - a
            # visibly different response when rate-limited would leak
            # its own bit of information.
            ip_limited = is_rate_limited(
                f"pwreset_ip:{client_ip(request)}",
                limit=PASSWORD_RESET_IP_RATE_LIMIT,
                window_seconds=3600,
                policy=SECURITY_CRITICAL,
            )
            email_limited = is_rate_limited(
                f"pwreset_email:{email.lower()}",
                limit=PASSWORD_RESET_EMAIL_RATE_LIMIT,
                window_seconds=3600,
                policy=SECURITY_CRITICAL,
            )
            if not ip_limited and not email_limited:
                for user in form.get_users(email):
                    _send_password_reset_email(request, user)
            # Same message regardless of whether an account exists (or
            # this request got rate-limited), so this can't be used to
            # check whether an email is registered.
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
            # Same reasoning as the in-profile user.password_change entry:
            # a credential change is exactly what an account-takeover
            # investigation needs a timestamp for, and the emailed-link path
            # is the one an attacker with mailbox access would use. Actor is
            # the account itself (nobody is logged in yet); no value recorded.
            from governance.audit import log_action

            log_action(actor=user, action_type="user.password_reset_via_email", target=user)
            messages.success(request, translation.gettext("Your password has been reset. You can log in now."))
            return redirect("accounts:login")
    else:
        form = PortalSetPasswordForm(user=user)

    return render(request, "accounts/password_reset_confirm.html", {"form": form, "valid_link": True})


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/dashboard.html"

    def get_context_data(self, **kwargs):
        from governance.limits import get_usage_status
        from governance.plans import get_plan_status, plan_capability_summary

        context = super().get_context_data(**kwargs)
        context["user"] = self.request.user
        context["admin_setup_checklist"] = self._admin_setup_checklist(self.request.user)
        # "Your plan"/"Your usage" cards on the landing page after login -
        # reuses chat/_usage_widget.html (previously only reachable via
        # the chat page's small header "Usage" popover), not a second
        # differently-behaved copy of it.
        plan_status = get_plan_status(self.request.user)
        context["plan_status"] = plan_status
        # "What's included" on the current plan itself - the SAME per-
        # plan capability list billing:my_plans/billing/_plan_cards.html
        # shows for every plan (billing/_plan_capability_list.html),
        # reused here for just the one the user is actually on.
        context["current_plan_capabilities"] = (
            plan_capability_summary(plan_status["plan"]) if plan_status["plan"] else []
        )
        context["usage"] = get_usage_status(self.request.user)
        return context

    @staticmethod
    def _admin_setup_checklist(user):
        """A brand-new department Admin otherwise has to discover Users/
        Billing Profile/System Prompt on their own by browsing the nav
        (Gap 15 - onboarding) - this surfaces the 3 setup steps directly
        on first login. Returns None (renders nothing) once every item is
        done, or for anyone who isn't a department Admin at all - it's a
        one-time nudge, not a permanent dashboard fixture."""
        if user.role != User.Role.ADMIN or not user.department_id:
            return None

        from governance.features import user_has_feature

        department = user.department
        items = [
            {
                "label": translation.gettext("Add your team"),
                "url": reverse("governance:users"),
                "done": department.users.exclude(pk=user.pk).exists(),
            },
        ]
        # Billing Profile/System Prompt are both gated behind the same
        # role-wide "department_settings" feature (see governance's
        # SystemPromptView/billing's DepartmentBillingProfileView) - if a
        # SuperAdmin has hidden that from Admins, linking to it here would
        # just 403. "Add your team" alone still stands on its own.
        if user_has_feature(user, "department_settings"):
            from billing.models import DepartmentBillingProfile
            from governance.models import SystemPromptVersion

            items += [
                {
                    "label": translation.gettext("Set up your billing profile"),
                    "url": reverse("billing:department_billing_profile", kwargs={"department_id": department.id}),
                    "done": DepartmentBillingProfile.objects.filter(department=department).exists(),
                },
                {
                    "label": translation.gettext("Customize your system prompt"),
                    "url": reverse("governance:system_prompt", kwargs={"department_id": department.id}),
                    "done": SystemPromptVersion.objects.filter(department=department, is_active=True).exists(),
                },
            ]

        if all(item["done"] for item in items):
            return None
        return items


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
            from governance.audit import log_action

            log_action(actor=request.user, action_type="user.password_change", target=request.user)
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
