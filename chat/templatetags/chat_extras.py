from django import template
from django.utils.safestring import mark_safe

from chat.markdown_utils import render_markdown

register = template.Library()


@register.filter(name="render_markdown", is_safe=True)
def render_markdown_filter(text):
    return mark_safe(render_markdown(text))


@register.filter(name="plan_has_feature")
def plan_has_feature(user, feature_key):
    """{{ user|plan_has_feature:"document_generation" }} - checks
    governance.plans.has_feature (the per-PLAN subscription grant,
    Plan.feature_flags/KNOWN_FEATURE_FLAGS). NOT the same thing as the
    governance_extras `has_feature` filter already used throughout this
    app, which checks the unrelated per-ROLE nav-visibility switch
    (RoleFeatureToggle) - deliberately a different name, not an override,
    so a template using one can never be silently checking the other."""
    from governance.plans import has_feature as plan_level_has_feature

    if not getattr(user, "is_authenticated", False):
        return False
    return plan_level_has_feature(user, feature_key)


@register.filter(name="to_offset")
def to_offset(pct):
    """Circumference-100 SVG ring: dashoffset needed to reveal `pct`
    percent of the stroke, for the fill-in animation in _usage_ring.html."""
    try:
        return max(0, 100 - int(round(float(pct))))
    except (TypeError, ValueError):
        return 100
