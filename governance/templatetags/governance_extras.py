from django import template

register = template.Library()


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
