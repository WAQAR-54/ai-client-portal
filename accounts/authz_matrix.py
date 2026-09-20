"""Helpers that discover every URL route and probe it as each role.

Used by governance/test_authorization_matrix.py. Kept out of the test module so the
same inventory can be dumped for review (set AUTHZ_DUMP=<path> when running that test).

A route is probed with placeholder ids that match nothing (999999 / "x"), so a request
can only reveal WHETHER THE ROLE IS LET IN (what the view's permission check decides
before it looks anything up), never touch another user's object. Object-level
isolation is tested separately, with real objects.
"""

import re

from django.urls import URLPattern, URLResolver, get_resolver

_PARAM = re.compile(r"<(?:(?P<conv>int|str|slug|uuid|path):)?(?P<name>\w+)>")
# Routes that are intentionally public (login flow, health, static-ish). Everything else
# must refuse an anonymous visitor.
PUBLIC_PREFIXES = (
    "/accounts/login/",
    "/accounts/signup/",
    "/accounts/logout/",
    "/accounts/password-reset/",
    "/accounts/reset/",
    "/accounts/google/",
    "/accounts/mfa/",
    "/accounts/verify/",
    "/healthz/",
    "/client-errors/",  # log-only browser error beacon (config/client_errors.py): must work with an expired session
    "/static/",
    "/i18n/",
)


def _fill(route):
    def value(match):
        return "999999" if match.group("conv") in (None, "int") else "x"

    return _PARAM.sub(value, route)


def iter_routes(patterns=None, prefix=""):
    """Yield (name, path) for every route, with placeholder ids substituted."""
    for pattern in patterns if patterns is not None else get_resolver().url_patterns:
        route = str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            if route.startswith("^") or "(?P" in route:  # regex-mounted trees (media/docs/static)
                continue
            yield from iter_routes(pattern.url_patterns, prefix + route)
        elif isinstance(pattern, URLPattern):
            if route.startswith("^") or "(?P" in route:
                continue
            yield pattern.name or "", "/" + _fill(prefix + route).lstrip("/")


def is_public(path):
    return path.startswith(PUBLIC_PREFIXES)


def classify(response):
    """Reduce a response to what matters for authorization."""
    status = response.status_code
    if status in (401, 403):
        return "DENY"
    if status in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "")
        return "LOGIN" if "/accounts/login" in location or "/login/" in location else "REDIRECT"
    if status == 404:
        return "404"
    if status >= 500:
        return "ERROR"
    return "ALLOW"


def probe(client, path):
    """(method, verdict). GET first; POST when the route only accepts POST."""
    response = client.get(path)
    method = "GET"
    if response.status_code == 405:
        response, method = client.post(path, {}), "POST"
    return method, classify(response), response.status_code, response.headers.get("Location", "")
