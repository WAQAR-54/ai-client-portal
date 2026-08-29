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
