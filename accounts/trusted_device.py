"""Trusted device: one browser per user may skip the e-mail MFA step.

* The device is identified by a random 256-bit token kept in an HttpOnly, Secure, SameSite=Lax cookie. The database
  stores only its SHA-256 hash. No fingerprinting, no IP, no user-agent matching decides anything (the user agent is
  only turned into a human label such as "Chrome on Windows").
* A different browser, a different computer, cleared cookies or a private window have no valid token, so MFA runs.
* After a successful MFA the new device becomes the ONLY trusted one (older ones are revoked) and, because that login
  goes through django's login(), the existing single-session rule (accounts/single_session.py) signs the older sessions
  out. "Sign out all sessions" revokes the device and every session, so the next sign-in needs MFA again.
"""

import hashlib
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

COOKIE_NAME = "trusted_device"


def _days():
    return int(getattr(settings, "TRUSTED_DEVICE_DAYS", 30))


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def device_label(request):
    """ "Chrome on Windows": browser and OS family only, nothing that identifies a person or a machine."""
    ua = request.META.get("HTTP_USER_AGENT", "")[:300]
    browser = next(
        (
            n
            for p, n in (
                ("Edg/", "Edge"),
                ("OPR/", "Opera"),
                ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if p in ua
        ),
        "Browser",
    )
    system = next(
        (
            n
            for p, n in (
                ("Windows", "Windows"),
                ("Android", "Android"),
                ("iPhone", "iOS"),
                ("iPad", "iOS"),
                ("Mac OS X", "macOS"),
                ("Linux", "Linux"),
            )
            if re.search(p, ua)
        ),
        "",
    )
    return f"{browser} on {system}" if system else browser


def active_device(user):
    from accounts.models import TrustedDevice

    now = timezone.now()
    return TrustedDevice.objects.filter(user=user, revoked_at__isnull=True, expires_at__gt=now).first()


def is_trusted(request, user):
    """True when this request carries the valid token of the user's active trusted device (and records the use)."""
    from accounts.models import TrustedDevice

    token = request.COOKIES.get(COOKIE_NAME, "")
    if not token or len(token) > 200:
        return False
    now = timezone.now()
    device = TrustedDevice.objects.filter(
        user=user, token_hash=_hash(token), revoked_at__isnull=True, expires_at__gt=now
    ).first()
    if device is None:
        return False
    TrustedDevice.objects.filter(pk=device.pk).update(last_used_at=now)
    return True


def revoke_all(user):
    from accounts.models import TrustedDevice

    return TrustedDevice.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())


def register(request, response, user):
    """Make THIS browser the user's only trusted device: revoke the rest, store the token's hash, set the cookie."""
    from accounts.models import TrustedDevice

    revoke_all(user)
    token = secrets.token_urlsafe(32)
    now = timezone.now()
    device = TrustedDevice.objects.create(
        user=user,
        token_hash=_hash(token),
        label=device_label(request),
        last_used_at=now,
        expires_at=now + timedelta(days=_days()),
    )
    set_cookie(response, token)
    return device


def set_cookie(response, token):
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=_days() * 86400,
        secure=not settings.DEBUG,  # HTTPS only (plain http is allowed just for local development)
        httponly=True,
        samesite="Lax",
    )


def clear_cookie(response):
    response.delete_cookie(COOKIE_NAME, samesite="Lax")


def notify_new_device(user, device):
    """The existing notification + global e-mail shell: "a new trusted device was registered". Never raises."""
    import logging

    from django.utils import translation

    try:
        from notifications.models import NotificationType
        from notifications.notify import notify

        with translation.override(user.preferred_language):
            title = translation.gettext("New trusted device")
            body = translation.gettext(
                "%(device)s is now your trusted device and skips the verification code. Any earlier trusted device "
                "and its sessions were signed out. If this wasn't you, change your password and use "
                "Sign out all sessions in Settings."
            ) % {"device": device.label or "A browser"}
        notify(user, NotificationType.NEW_TRUSTED_DEVICE, title, body)
    except Exception:  # noqa: BLE001 - a notification problem must never block a sign-in
        logging.getLogger(__name__).warning("New trusted device notice could not be sent", exc_info=True)
