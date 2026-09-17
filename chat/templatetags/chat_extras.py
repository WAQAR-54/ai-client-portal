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


# The 5 providers static/css/main.css has a hand-written .model-badge-{slug}/
# .provider-dot-{slug} pair for today (see main.css ~2324-2329, ~2496-2501).
# Any OTHER slug (a newly-connected provider, or a custom OpenAI-compatible
# one) falls through to the *-dynamic classes below, which read their color
# from an inline --provider-accent custom property (Provider.accent_color())
# instead of a hardcoded CSS rule - so a 6th+ provider is never stuck with
# the flat gray *-default look just because no CSS rule was hand-written
# for its slug.
_KNOWN_PROVIDER_SLUGS = {"anthropic", "openai", "gemini", "grok", "deepseek"}


@register.filter(name="provider_badge_class")
def provider_badge_class(slug):
    if not slug:
        return "model-badge-default"
    return f"model-badge-{slug}" if slug in _KNOWN_PROVIDER_SLUGS else "model-badge-dynamic"


@register.filter(name="provider_dot_class")
def provider_dot_class(slug):
    if not slug:
        return "provider-dot-default"
    return f"provider-dot-{slug}" if slug in _KNOWN_PROVIDER_SLUGS else "provider-dot-dynamic"


@register.filter(name="to_offset")
def to_offset(pct):
    """Circumference-100 SVG ring: dashoffset needed to reveal `pct`
    percent of the stroke, for the fill-in animation in _usage_ring.html."""
    try:
        return max(0, 100 - int(round(float(pct))))
    except (TypeError, ValueError):
        return 100
