"""One place that decides whether a user-supplied `next` URL may be redirected to.

Several POST views ended with `redirect(request.POST.get("next") or "<default>")`, which
sends the browser to ANY address the form field names (an open redirect: usable to make a
link on this domain end up on a look-alike login page). Only same-host URLs are followed.
"""

from django.utils.http import url_has_allowed_host_and_scheme


def safe_next_url(request, default, param="next"):
    """The posted `next` when it points at this site, otherwise `default` (a URL or a URL name,
    exactly as `redirect()` accepts it)."""
    candidate = (request.POST.get(param) or "").strip()
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return default
