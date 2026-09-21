"""Request-level Maintenance Mode enforcement (rules and states: governance/maintenance.py)."""

import logging
from functools import lru_cache

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse

from governance import maintenance

logger = logging.getLogger(__name__)

# Always reachable, maintenance or not:
#  * the health endpoints (/healthz/, /healthz/deep/): Docker, the deploy check and monitors must keep seeing the truth
#  * static files and the uploaded brand logos: the maintenance page itself is branded
_ALWAYS_OPEN_PREFIXES = ("/healthz/", "/favicon.ico")
# The sign-in flow, so a SuperAdmin can get in (login, the MFA step, Google sign-in) and anyone can sign out. Nothing
# here reveals whether an account exists: they behave exactly as they do outside maintenance, and a signed-in user who
# is not a SuperAdmin simply sees the maintenance page on the next request.
_SIGN_IN_URL_NAMES = (
    "accounts:login",
    "accounts:logout",
    "accounts:mfa_verify",
    "accounts:resend_mfa_code",
    "accounts:google_signin",
)


def _open_paths():
    static = "/" + settings.STATIC_URL.strip("/") + "/" if settings.STATIC_URL else "/static/"
    media = (
        "/" + settings.MEDIA_URL.strip("/") + "/branding/" if getattr(settings, "MEDIA_URL", "") else "/media/branding/"
    )
    return _ALWAYS_OPEN_PREFIXES + (static, media)


@lru_cache(maxsize=1)
def _sign_in_paths():
    return frozenset(reverse(name) for name in _SIGN_IN_URL_NAMES)


def _is_open_path(path):
    return path.startswith(_open_paths()) or path in _sign_in_paths()


class MaintenanceMiddleware:
    """While a maintenance window is ACTIVE every visitor sees the branded maintenance page (HTTP 503, Retry-After,
    never cached) - normal users, Admins, department Admins, and anonymous visitors alike - whatever URL they ask
    for, so a bookmarked or typed link is blocked as firmly as a hidden menu item. SuperAdmins carry on as normal.

    After AuthenticationMiddleware (it needs request.user). When the check itself fails, the site stays open."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _is_open_path(request.path):
            return self.get_response(request)
        state = maintenance.current_state()
        if state is None:
            return self.get_response(request)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.role == user.Role.SUPERADMIN:
            return self.get_response(request)
        return self._maintenance_response(request, state)

    def _maintenance_response(self, request, state):
        if request.headers.get("HX-Request"):
            # htmx would not swap a 503 body in; send the whole page to the maintenance screen instead.
            response = HttpResponse(status=204)
            response["HX-Redirect"] = "/"
            response["Cache-Control"] = "no-store"
            return response
        response = render(request, "maintenance.html", maintenance.page_context(state), status=503)
        response["Cache-Control"] = "no-store"
        retry = 300
        if state.get("end"):
            from django.utils import timezone

            retry = max(60, int(state["end"] - timezone.now().timestamp()))
        response["Retry-After"] = str(min(retry, 3600))
        return response
