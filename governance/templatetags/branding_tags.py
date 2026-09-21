from django import template
from django.utils.html import format_html

from governance import branding

register = template.Library()


@register.simple_tag
def brand_email():
    """{% brand_email as brand %} - the ACTIVE branding as concrete colours (brand.primary, brand.text, ...), for
    emails, invoices and PDFs, which cannot use CSS variables and are rendered without a request (Celery tasks)."""
    return branding.email_tokens()


@register.simple_tag
def brand_last_known():
    """{% brand_last_known as known %} - name / logo urls of the last rendered branding (cache only, no database)."""
    return branding.last_known_branding()


@register.simple_tag
def brand_head_cached():
    """The fonts link and token <style> of the last branding that was rendered, read from the cache only: for pages
    that must render with the database possibly down (the error page, the maintenance page). Branding 1 (nothing) if
    no page has been rendered yet."""
    known = branding.last_known_branding()
    out = ""
    if known.get("fonts_url"):
        out += format_html('<link href="{}" rel="stylesheet">', known["fonts_url"])
    if known.get("css"):
        out += format_html('<style id="brand-tokens">{}</style>', template.base.mark_safe(known["css"]))
    return template.base.mark_safe(out)


@register.filter
def dict_get(mapping, key):
    return mapping.get(key, "") if hasattr(mapping, "get") else ""
