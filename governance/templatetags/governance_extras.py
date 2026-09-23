import os
import re

from django import template
from django.contrib.staticfiles import finders

register = template.Library()


@register.simple_tag
def static_v(path):
    """{% static_v 'css/main.css' %} - same as {% static %} but appends the
    file's own mtime as a ?v= query string, so a browser can never serve a
    stale cached copy after a CSS/JS change: dev's StaticFilesStorage (and
    prod's WhiteNoise, pre-manifest-lookup) both otherwise emit the exact
    same URL for every deploy, with no cache-busting of their own."""
    from django.templatetags.static import static as static_url

    url = static_url(path)
    found = finders.find(path)
    if found:
        try:
            url = f"{url}?v={int(os.path.getmtime(found))}"
        except OSError:
            pass
    return url


@register.filter
def dict_get(mapping, key):
    """{{ some_dict|dict_get:some_var }} - Django's `.` lookup can't take a
    variable key, so a dict keyed by e.g. user id needs this to look up
    per-row in a loop."""
    if mapping is None:
        return None
    return mapping.get(key)


@register.filter
def has_feature(user, feature_key):
    """{{ request.user|has_feature:"teams" }} - see governance/features.py.
    Gates template-level visibility; the matching view/endpoint enforces
    the same check server-side, so this is never the only thing standing
    between a role and a hidden feature."""
    from governance.features import user_has_feature

    return user_has_feature(user, feature_key)


_PLAN_TIER_CLASSES = {
    "demo": "badge-plan-demo",
    "basic": "badge-plan-basic",
    "advanced": "badge-plan-advanced",
    "full": "badge-plan-full",
}


@register.filter
def plan_tier_class(plan_name):
    """{{ plan.name|plan_tier_class }} - Plan.name is free text, not an
    enum, but the app's seeded plans use exactly these 4 names. Falls back
    to the generic muted badge for any other/custom plan name rather than
    guessing a color for it."""
    if not plan_name:
        return "badge-muted"
    return _PLAN_TIER_CLASSES.get(plan_name.strip().lower(), "badge-muted")


_AUDIT_DANGER_MARKERS = ("delete", "suspend", "block", "disable", "dismiss", "deny")
_AUDIT_WARN_MARKERS = ("downgrade", "expire", "warn")


@register.filter
def audit_severity(action_type):
    """{{ log.action_type|audit_severity }} -> "info"/"warn"/"danger", used
    to color a .log-dot per audit row. Classified from the action_type
    string itself (there's no severity field on AuditLog) rather than
    enumerating every action_type by hand, so a new action type not yet
    seen here still gets a reasonable default (info) instead of erroring."""
    if not action_type:
        return "info"
    lowered = action_type.lower()
    if any(marker in lowered for marker in _AUDIT_DANGER_MARKERS):
        return "danger"
    if any(marker in lowered for marker in _AUDIT_WARN_MARKERS):
        return "warn"
    return "info"


# old_value/new_value are plain TextFields - governance/audit.py::log_action just does
# str(old_value)/str(new_value), so nothing on the model stops a careless call site from ever
# passing something secret-shaped. Every real call site today passes short human-readable text
# (counts, plan names, region codes, "key ending 1234") - none currently need masking, but this is
# a safety net for the Audit Explorer's own display, not a claim that today's data is unsafe.
_SECRET_LIKE_RE = re.compile(r"^[A-Za-z0-9_\-+/=]{20,}$")


@register.filter
def mask_audit_value(value):
    """{{ log.old_value|mask_audit_value }} - redacts a value that LOOKS like a token/key/hash
    (one long run of characters with no whitespace and no @, so a real email/name/short phrase is
    never masked) before it ever reaches the template. Apply BEFORE truncatechars, not after -
    truncation's own "..." would otherwise break the full-string match this relies on."""
    if not value:
        return value
    stripped = value.strip()
    if "@" in stripped or " " in stripped:
        return value
    if _SECRET_LIKE_RE.match(stripped):
        return "•••• (masked — looked like a token/key)"
    return value
