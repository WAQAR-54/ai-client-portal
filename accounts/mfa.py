"""Email-OTP MFA: a short-lived 6-digit code sent to the user's real email
via the existing tracked-email pipeline, entered on a dedicated verify page
before the actual Django login (auth.login()) ever runs. No authenticator
app/QR code - deliberately simpler, reusing infrastructure that already
exists (see notifications.emailing.send_tracked_email).

State lives entirely in the pending (pre-login) session - never a DB table -
since a login-flow OTP has no reason to outlive the session it was issued
in. Session keys used: mfa_user_id, mfa_code, mfa_expires_at, mfa_next,
mfa_attempts.
"""

import random
from datetime import datetime, timedelta

from django.utils import timezone

OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 10
MAX_MFA_ATTEMPTS = 5


def generate_otp():
    return f"{random.randint(0, 10**OTP_LENGTH - 1):0{OTP_LENGTH}d}"


def user_requires_mfa(user):
    """Admin/SuperAdmin always go through MFA (user.is_admin covers both) -
    it's not a per-user choice for them, so their own mfa_enabled value is
    never consulted. User/Manager only go through it if they've turned it
    on themselves (see accounts/views.py::toggle_own_mfa)."""
    return user.is_admin or user.mfa_enabled


def start_mfa_challenge(request, user, next_url):
    """Stores a fresh code + expiry in the (pre-login) session and returns
    it so the caller can send the actual email - kept as a separate step
    (rather than sending here too) so tests can call this without needing
    to mock email sending."""
    code = generate_otp()
    request.session["mfa_user_id"] = user.id
    request.session["mfa_code"] = code
    request.session["mfa_expires_at"] = (timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()
    request.session["mfa_next"] = next_url or ""
    request.session["mfa_attempts"] = 0
    return code


def clear_mfa_session(request):
    for key in ("mfa_user_id", "mfa_code", "mfa_expires_at", "mfa_next", "mfa_attempts"):
        request.session.pop(key, None)


def mfa_challenge_expired(request):
    expires_at = request.session.get("mfa_expires_at")
    if not expires_at:
        return True
    return timezone.now() > datetime.fromisoformat(expires_at)
