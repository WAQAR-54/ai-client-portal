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
