"""Email-OTP MFA: a short-lived 6-digit code sent to the user's real email
via the existing tracked-email pipeline, entered on a dedicated verify page
before the actual Django login (auth.login()) ever runs. No authenticator
app/QR code - deliberately simpler, reusing infrastructure that already
exists (see notifications.emailing.send_tracked_email).

State lives entirely in the pending (pre-login) session - never a DB table -
since a login-flow OTP has no reason to outlive the session it was issued
in. Session keys used: mfa_user_id, mfa_code, mfa_expires_at, mfa_next,
mfa_attempts, mfa_resend_count.
"""

import secrets
from datetime import datetime, timedelta

from django.utils import timezone

OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 10
MAX_MFA_ATTEMPTS = 5
# Caps the resend+retry cycle (accounts/views.py::resend_mfa_code) - each
# resend hands out a fresh code with its own MAX_MFA_ATTEMPTS guesses, so
# without this cap someone who keeps requesting a new code before ever
# hitting the attempt limit could brute-force indefinitely instead of
# ever being forced back through a real login. Session-tracked (like
# mfa_attempts), reset only when a genuinely fresh challenge starts
# (PortalLoginView.form_valid), never by start_mfa_challenge itself - see
# that function's own docstring for why.
MAX_MFA_RESENDS = 3


def generate_otp():
    # secrets, not random: this is a security code, not a coin flip -
    # Python's random module is a Mersenne Twister PRNG, predictable
    # given enough observed output, which a 6-digit MFA code must not be.
    return f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"


def user_requires_mfa(user):
    """Admin/SuperAdmin go through MFA whenever SecuritySettings.load().
    mfa_required_for_admins is on (user.is_admin covers both roles) - not
    a per-user choice for them in that case, so their own mfa_enabled
    value is never consulted. Any other role only goes through it if
    they've turned it on themselves (see accounts/views.py::
    toggle_own_mfa), regardless of that setting - it only ever makes MFA
    mandatory, never blocks an opt-in.

    A real DB row (toggled from the Feature Visibility admin page), not a
    Django setting/env var - deliberately, so a SuperAdmin can flip it
    without needing server/SSH access."""
    from governance.models import SecuritySettings

    return (SecuritySettings.load().mfa_required_for_admins and user.is_admin) or user.mfa_enabled


def start_mfa_challenge(request, user, next_url):
    """Stores a fresh code + expiry in the (pre-login) session and returns
    it so the caller can send the actual email - kept as a separate step
    (rather than sending here too) so tests can call this without needing
    to mock email sending.

    Deliberately does NOT touch mfa_resend_count - whether this call is a
    genuinely fresh challenge (which should reset it to 0) or a resend
    (which should increment+cap it first) is the CALLER's decision
    (PortalLoginView.form_valid vs accounts/views.py::resend_mfa_code
    respectively), never guessed here. Collapsing that distinction into
    "reset every time this runs" was exactly the gap that let an
    unlimited resend+retry cycle bypass MAX_MFA_ATTEMPTS entirely."""
    code = generate_otp()
    request.session["mfa_user_id"] = user.id
    request.session["mfa_code"] = code
    request.session["mfa_expires_at"] = (timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()
    request.session["mfa_next"] = next_url or ""
    request.session["mfa_attempts"] = 0
    return code


def clear_mfa_session(request):
    for key in ("mfa_user_id", "mfa_code", "mfa_expires_at", "mfa_next", "mfa_attempts", "mfa_resend_count"):
        request.session.pop(key, None)


def mfa_challenge_expired(request):
    expires_at = request.session.get("mfa_expires_at")
    if not expires_at:
        return True
    return timezone.now() > datetime.fromisoformat(expires_at)
