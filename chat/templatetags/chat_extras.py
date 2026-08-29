from django import template
from django.utils.safestring import mark_safe

from chat.markdown_utils import render_markdown

register = template.Library()


@register.filter(name="render_markdown", is_safe=True)
def render_markdown_filter(text):
    return mark_safe(render_markdown(text))


@register.filter(name="to_offset")
def to_offset(pct):
    """Circumference-100 SVG ring: dashoffset needed to reveal `pct`
    percent of the stroke, for the fill-in animation in _usage_ring.html."""
    try:
        return max(0, 100 - int(round(float(pct))))
    except (TypeError, ValueError):
        return 100
