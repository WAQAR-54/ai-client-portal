"""Invoice PDF export - same xhtml2pdf/pisa call as chat/export.py's
render_conversation_pdf, but built from a real template (billing/templates/
billing/invoice_pdf.html) rather than hand-concatenated strings: an invoice
has more structure (header, itemized table, footer grid) than a flat
message loop, and xhtml2pdf's CSS support is limited enough (no flexbox/
grid) that the PDF needs its own table-based layout distinct from the
on-screen invoice_detail.html.
"""

import os
from io import BytesIO

from django.conf import settings
from django.template.loader import render_to_string
from xhtml2pdf import pisa

from billing.models import DepartmentBillingProfile, OrganizationBillingProfile
from governance.models import SiteBranding


def _resolve_pdf_uri(uri, _rel):
    """xhtml2pdf can't fetch MEDIA_URL/STATIC_URL paths itself - it needs a
    real filesystem path for every <img src>, which is why the uploaded
    SiteBranding logo silently failed to render (only a stderr warning,
    no exception) before this callback existed."""
    if uri.startswith(settings.MEDIA_URL):
        path = os.path.join(settings.MEDIA_ROOT, uri[len(settings.MEDIA_URL) :])
    elif uri.startswith(settings.STATIC_URL):
        path = os.path.join(settings.STATIC_ROOT or "", uri[len(settings.STATIC_URL) :])
    else:
        return uri
    return path if os.path.isfile(path) else uri


def render_invoice_pdf(invoice) -> bytes:
    # render_to_string with no `request=` never runs context processors
    # (that's how SiteBranding normally reaches every template), and this
    # will eventually be called from a request-less context too (the
    # Celery Beat sweep task planned for later) - so site_branding is
    # fetched and passed explicitly rather than relied on implicitly.
    # None for a department-less invoice (generate_invoice_for_user) -
    # DepartmentBillingProfile is a OneToOneField to Department, so
    # get_or_create(department=None) would violate its NOT NULL column.
    billing_profile = None
    if invoice.department_id is not None:
        billing_profile, _created = DepartmentBillingProfile.objects.get_or_create(department=invoice.department)
    html = render_to_string(
        "billing/invoice_pdf.html",
        {
            "invoice": invoice,
            "billing_profile": billing_profile,
            "organization_profile": OrganizationBillingProfile.load(),
            "site_branding": SiteBranding.load(),
        },
    )
    buffer = BytesIO()
    status = pisa.CreatePDF(html, dest=buffer, link_callback=_resolve_pdf_uri)
    if status.err:
        raise ValueError(f"PDF generation failed for invoice {invoice.id}")
    return buffer.getvalue()
