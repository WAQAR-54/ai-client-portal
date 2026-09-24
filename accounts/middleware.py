import json
import logging
import uuid
from contextvars import ContextVar

import sentry_sdk
from celery import current_task
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.http import HttpResponse, HttpResponsePermanentRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone, translation

from accounts.geo import language_for_ip
from accounts.rate_limit import client_ip

# Set by RequestIDMiddleware for the lifetime of one request, read by
# RequestIDLogFilter (below) so every log record - including ones logged
# deep inside chat/providers.py or chat/views.py, with no request object in
# scope - can be tagged without threading an id through every function
# signature. A ContextVar (not a plain module global) so it can't leak
# across requests handled concurrently by the same async/threaded worker.
_current_request_id: ContextVar[str] = ContextVar("current_request_id", default="-")

# A plain dict that lives for exactly one request - see get_request_cache()
# below.
_request_local_cache: ContextVar = ContextVar("request_local_cache", default=None)


def get_request_cache():
    """A dict scoped to exactly one request, for memoizing small, read-
    mostly lookups that would otherwise run once per row on a list template
    (e.g. governance/features.py::role_has_feature, called once per
    conversation row per feature key - a real N+1 the audit measured
    directly). Deliberately NOT a cross-request cache (Django's cache
    framework, a TTL, etc.): a role/feature toggle is a live access-control
    decision, and a cross-request cache either goes stale for its whole TTL
    after an admin flips it, or - as found while first building this with
    Django's cache backend - leaks a cached answer from one Django TestCase
    into a later, unrelated one, since LocMemCache isn't rolled back by
    TestCase's transaction rollback the way the DB is. Scoping the cache to
    one request's ContextVar sidesteps both: it can never outlive the
    request that populated it, in production or in a test.

    Returns None outside of a request (a management command, a direct
    Python call in a test with no self.client.get/post involved) - callers
    must treat that as "don't memoize this call", not as a cache miss."""
    return _request_local_cache.get()


class RequestIDMiddleware:
    """Generates a short id for every request and makes it available to
    logging (via RequestIDLogFilter), to Sentry (as a tag - the "Request ID"
    field an operator can search on directly), and back to the client (the
    X-Request-ID response header - useful for support: "what's in your
    browser's network tab for this failed request" now maps directly to a
    log line and a Sentry event).

    Deliberately always generates fresh rather than trusting an inbound
    X-Request-ID header from the client - the reverse proxy in front of this
    app isn't confirmed to strip that header, and trusting a client-supplied
    value here would let it inject arbitrary text into every log line and
    Sentry event for that request.

    Does NOT reach into Celery: a task queued from within a request (e.g.
    notify()'s send_notification_email) gets its own, separate identity from
    Celery itself (self.request.id) - see RequestIDLogFilter, which surfaces
    that instead when running inside a worker. Chaining "which request
    caused this task" end-to-end would mean threading this id through every
    .delay() call site across notifications/billing/governance - out of
    scope for this pass; each half (request, task) is independently
    traceable, but not yet joined.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.id = uuid.uuid4().hex[:16]
        token = _current_request_id.set(request.id)
        sentry_sdk.set_tag("request_id", request.id)
        # Unlike _current_request_id below, this dict is only ever read
        # during synchronous template rendering inside get_response() itself
        # (role_has_feature has no reason to run inside stream_message's
        # lazy SSE generator) - a plain try/finally around get_response() is
        # correct here, no close()-hook needed.
        cache_token = _request_local_cache.set({})
        try:
            response = self.get_response(request)
        finally:
            _request_local_cache.reset(cache_token)
        response["X-Request-ID"] = request.id
        # NOT a finally around get_response(): for a StreamingHttpResponse
        # (chat/views.py::stream_message), get_response() returns the
        # response object immediately, unconsumed - the generator body
        # (where a provider failure actually gets logged) only runs later,
        # when the WSGI server iterates it, which is AFTER this middleware's
        # __call__ has already returned. Resetting here would clear the id
        # before that logging call ever happens, defeating the one case
        # this exists for. response.close() (Django/WSGI's own hook, fired
        # once the response - streaming or not - is fully sent) is the
        # actual end of this request's lifetime.
        response._resource_closers.append(lambda: _current_request_id.reset(token))
        return response


class RequestIDLogFilter(logging.Filter):
    """Attached to every handler in LOGGING (config/settings.py) so the
    formatter can include %(request_id)s on every line, whether or not the
    code that logged it has a request object in scope."""

    def filter(self, record):
        record.request_id = _current_request_id.get()
        record.task_id = current_task.request.id if current_task else "-"
        return True


class GeoLanguageMiddleware:
    """Picks a starting UI language from the visitor's IP country, for
    visitors who haven't chosen a language yet (no language cookie set).
    This only ever sets the *initial* guess - once a cookie exists (the
    visitor picked one, or this middleware already set one on an earlier
    request) it's left alone. For logged-in users it's harmless busywork at
    worst: UserLanguagePreferenceMiddleware's DB-stored preference always
    wins over whatever this or LocaleMiddleware guessed, further down the
    chain.

    Must run after SessionMiddleware and before LocaleMiddleware - it mutates
    request.COOKIES so LocaleMiddleware's own cookie-based detection (right
    after it in the chain) picks up the guess on this same request, then sets
    a real Set-Cookie on the response so the guess sticks for subsequent
    requests without a lookup every time. Deliberately doesn't key off
    request.user: AuthenticationMiddleware (which populates it) runs later in
    the chain than LocaleMiddleware requires this middleware to sit, so
    request.user isn't available yet here.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        cookie_name = settings.LANGUAGE_COOKIE_NAME
        detected = None
        if cookie_name not in request.COOKIES:
            detected = language_for_ip(client_ip(request))
            request.COOKIES[cookie_name] = detected

        response = self.get_response(request)

        if detected:
            response.set_cookie(cookie_name, detected)
        return response


class UserLanguagePreferenceMiddleware:
    """Makes the logged-in user's stored language preference
    (User.preferred_language) the active UI language for every request they
    make, regardless of cookies/browser - that's what makes the choice
    persist across logins and devices rather than being tied to one
    browser's cookie jar, per the spec ("not just per-session").

    Must run after AuthenticationMiddleware (needs request.user) and after
    LocaleMiddleware (this deliberately overrides its cookie/header-based
    guess for authenticated users) - see the MIDDLEWARE ordering comment in
    config/settings.py.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            preferred = request.user.preferred_language
            if translation.get_language() != preferred:
                translation.activate(preferred)
                request.LANGUAGE_CODE = translation.get_language()
        return self.get_response(request)


SESSION_TIMEOUT_MINUTES = getattr(settings, "SESSION_TIMEOUT_MINUTES", 30)


class SessionTimeoutMiddleware:
    """Logs an inactive user out after SESSION_TIMEOUT_MINUTES of no
    requests - distinct from SESSION_COOKIE_AGE (an absolute session
    lifetime regardless of activity). Tracks a plain timestamp in the
    session itself rather than a DB table, since "how long has this
    browser session been idle" has no reason to outlive the session.

    Must run after AuthenticationMiddleware (needs request.user) and after
    MessageMiddleware (uses django.contrib.messages) - see the MIDDLEWARE
    ordering comment in config/settings.py.

    An htmx request gets HX-Redirect instead of a 302, same reasoning as
    SingleSessionMiddleware below: a background htmx poll (e.g. the
    notification bell's hx-trigger="load, every 45s" in base.html) can be
    the one that lands after the timeout with no user action at all, and a
    plain redirect would have htmx swap the whole rendered login page into
    that poll's small target element instead of replacing the page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            last_activity = request.session.get("last_activity")
            now = timezone.now().timestamp()
            if last_activity is not None and (now - last_activity) > SESSION_TIMEOUT_MINUTES * 60:
                logout(request)
                messages.info(request, translation.gettext("You were logged out after a period of inactivity."))
                login_url = reverse("accounts:login")
                if request.headers.get("HX-Request"):
                    response = HttpResponse(status=204)
                    response["HX-Redirect"] = login_url
                    return response
                return redirect(login_url)
            request.session["last_activity"] = now
        return self.get_response(request)


class SingleSessionMiddleware:
    """Signs out a browser whose account has since been signed in somewhere else (accounts/single_session.py).

    After MessageMiddleware (explains why) and AuthenticationMiddleware (needs request.user, whose token
    column is already loaded, so this adds no query for a current session). Anonymous requests and the
    SINGLE_SESSION_PER_USER=False setting pass straight through. An htmx request gets HX-Redirect instead of a
    302, so the login page replaces the whole page rather than being swapped into a fragment."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from accounts import single_session

        if single_session.enabled() and request.user.is_authenticated and not single_session.check_session(request):
            from governance.audit import log_action

            user = request.user
            log_action(
                actor=user, action_type="auth.session_superseded", target=user, new_value=f"ip={client_ip(request)}"
            )
            logout(request)
            messages.info(
                request,
                translation.gettext(
                    "You were signed out because your account was signed in on another browser or device."
                ),
            )
            login_url = reverse("accounts:login")
            if request.headers.get("HX-Request"):
                response = HttpResponse(status=204)
                response["HX-Redirect"] = login_url
                return response
            return redirect(login_url)
        return self.get_response(request)


class HtmxLoginRedirectMiddleware:
    """The general case SessionTimeoutMiddleware/SingleSessionMiddleware above don't cover: by the time
    either of those runs, request.user is still authenticated (this app's own idle-timeout/single-session
    logic is what logs them out, right there in that same request). But a session can already be
    anonymous when AuthenticationMiddleware runs - the session cookie expired or was deleted client-side,
    or the session was invalidated some other way (e.g. Profile > "Sign out all sessions" from another
    tab). Then request.user is AnonymousUser from the start, neither of those middlewares' `if
    request.user.is_authenticated:` guard ever fires, and the view's own @login_required/
    LoginRequiredMixin (75+ call sites across the app) returns Django's plain 302 to the login page -
    exactly the response an htmx background request (the notification bell's 45s poll, the admin topbar's
    search box, the Ctrl+K palette, anything else with an hx-get) would otherwise have its whole rendered
    login page swapped into, in place of that request's own tiny target element - a real reported bug,
    reproduced in a real browser: the dashboard/sidebar stayed on screen with the full login page's
    two-column layout squeezed into a poll's target div.

    Runs after the view (response-side only, no request-side work) so it doesn't matter where exactly in
    MIDDLEWARE this sits relative to the two middlewares above, as long as it's after
    AuthenticationMiddleware; grouped with them here since it's the same category of fix. Only touches an
    HX-Request whose response is a redirect landing on the login page - a normal browser navigation is
    untouched (a plain 302 there is already correct: the browser replaces the whole page itself), and any
    OTHER redirect (a view's own business-logic redirect, HX-Redirect responses the two middlewares above
    already return with a 204 rather than 302) never matches this check either."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.headers.get("HX-Request")
            and response.status_code in (301, 302)
            and response.get("Location", "").startswith(reverse("accounts:login"))
        ):
            htmx_response = HttpResponse(status=204)
            htmx_response["HX-Redirect"] = response["Location"]
            return htmx_response
        return response


def _visitor_scheme(request):
    """'http' or 'https' - the scheme of the VISITOR's connection to Cloudflare, from the CF-Visitor header
    Cloudflare adds to every request it proxies ({"scheme":"https"}). None when the header is absent or not
    what Cloudflare sends (a request that did not come through Cloudflare, e.g. the deploy's health check)."""
    raw = request.META.get("HTTP_CF_VISITOR", "")
    if not raw or len(raw) > 100:
        return None
    try:
        scheme = json.loads(raw).get("scheme")
    except (ValueError, AttributeError):
        return None
    return scheme if scheme in ("http", "https") else None


class CloudflareHttpsMiddleware:
    """HTTPS for visitors who arrive through Cloudflare, without needing Django to see TLS itself.

    Cloudflare terminates TLS and speaks plain HTTP to this origin, so request.is_secure() is False and
    Django's own SECURE_SSL_REDIRECT / HSTS cannot work (they would redirect forever or never fire). What
    Cloudflare does tell us, in CF-Visitor, is the scheme the VISITOR used:

    * visitor used http  -> redirect to the same URL on https (301 for GET/HEAD, 308 to keep the method), so
      a browser can never keep a session on plain HTTP. The two health endpoints are exempt, and requests
      with no CF-Visitor (the deploy's own health check, local dev) are never redirected.
    * visitor used https -> add Strict-Transport-Security, if CLOUDFLARE_HSTS_SECONDS > 0 (max-age only: no
      includeSubDomains and no preload, which are much harder to undo).

    Off by default (ENFORCE_HTTPS_VIA_CLOUDFLARE, CLOUDFLARE_HSTS_SECONDS); docker-compose.yml turns it on for
    production. There is no redirect loop: CF-Visitor reflects the visitor's scheme, not the origin's."""

    EXEMPT_PATHS = frozenset({"/healthz/", "/healthz/deep/"})

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        scheme = _visitor_scheme(request)
        if (
            getattr(settings, "ENFORCE_HTTPS_VIA_CLOUDFLARE", False)
            and scheme == "http"
            and request.path not in self.EXEMPT_PATHS
        ):
            target = f"https://{request.get_host()}{request.get_full_path()}"
            if request.method in ("GET", "HEAD"):
                return HttpResponsePermanentRedirect(target)
            response = HttpResponse(status=308)
            response["Location"] = target
            return response
        response = self.get_response(request)
        seconds = int(getattr(settings, "CLOUDFLARE_HSTS_SECONDS", 0) or 0)
        if seconds > 0 and scheme == "https" and "Strict-Transport-Security" not in response:
            response["Strict-Transport-Security"] = f"max-age={seconds}"
        return response
