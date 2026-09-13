"""Sign in with Google (Google Identity Services button widget) - the
browser gets a signed ID token (JWT) directly from Google and POSTs it to
accounts:google_signin; this module verifies that token server-side and
resolves it to a portal User. No OAuth redirect/callback dance and no
client secret needed - GIS's whole point is that the ID token itself,
verified against Google's own public keys, is already enough proof of
identity.

Deliberately a separate module, not accounts/views.py - same split as
accounts/mfa.py and accounts/rate_limit.py: business logic lives here, the
view that calls into it stays thin.
"""

from django.conf import settings
from django.utils import timezone

from accounts.models import User


def google_signin_enabled():
    """Whether the "Sign in with Google" button should even be offered -
    both a real Client ID configured (GOOGLE_OAUTH_CLIENT_ID, an env var,
    never committed) AND a SuperAdmin having explicitly turned it on
    (governance.models.SecuritySettings.google_signin_enabled, default
    off) - same reasoning as mfa_required_for_admins: don't switch on a
    login path before it's confirmed actually configured."""
    from governance.models import SecuritySettings

    return bool(settings.GOOGLE_OAUTH_CLIENT_ID) and SecuritySettings.load().google_signin_enabled


class GoogleSignInError(Exception):
    """Wraps every way verify_google_credential can fail into one type the
    view catches and turns into a user-facing message, without needing to
    know which underlying library call raised what."""


def verify_google_credential(credential):
    """Verifies the ID token's signature against Google's own current
    public keys, and its audience/issuer/expiry claims - all handled by
    google-auth's own verify_oauth2_token, not hand-rolled JWT parsing,
    since a mistake in that logic would mean accepting a forged login.
    Returns the decoded payload dict on success."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    try:
        payload = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), settings.GOOGLE_OAUTH_CLIENT_ID
        )
    except ValueError as exc:
        raise GoogleSignInError("Could not verify that Google sign-in - please try again.") from exc

    if not payload.get("email_verified"):
        raise GoogleSignInError("Your Google account's email address isn't verified.")
    return payload


def find_or_create_user_from_google(payload):
    """Resolves a verified Google payload to a portal User - by
    google_sub first (an already-linked account), falling back to a
    case-insensitive email match (someone who signed up with a password
    using this same email, now signing in with Google for the first time -
    this links the two rather than creating a duplicate account), and only
    creating a brand-new User if neither matches. Always re-stamps
    google_email/google_picture_url/google_linked_at, so a changed Google
    avatar or display name shows up on the very next sign-in without a
    separate sync step."""
    google_sub = payload["sub"]
    email = payload["email"].lower()

    user = User.objects.filter(google_sub=google_sub).first()
    if user is None:
        user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User(email=email, role=User.Role.USER)
        user.set_unusable_password()
        name = payload.get("name", "").strip()
        if name:
            first, _, last = name.partition(" ")
            user.first_name = first
            user.last_name = last

    user.google_sub = google_sub
    user.google_email = email
    user.google_picture_url = payload.get("picture", "") or ""
    user.google_linked_at = timezone.now()
    user.save()
    return user
