"""Invoice PDF export - same xhtml2pdf/pisa call as chat/export.py's
render_conversation_pdf, but built from a real template (billing/templates/
billing/invoice_pdf.html) rather than hand-concatenated strings: an invoice
has more structure (header, itemized table, footer grid) than a flat
message loop, and xhtml2pdf's CSS support is limited enough (no flexbox/
grid) that the PDF needs its own table-based layout distinct from the
on-screen invoice_detail.html.
"""

from io import BytesIO

from django.template.loader import render_to_string
from xhtml2pdf import pisa

from billing.models import DepartmentBillingProfile, OrganizationBillingProfile
from governance.models import SiteBranding


def render_invoice_pdf(invoice) -> bytes:
    # render_to_string with no `request=` never runs context processors
    # (that's how SiteBranding normally reaches every template), and this
    # will eventually be called from a request-less context too (the
    # Celery Beat sweep task planned for later) - so site_branding is
    # fetched and passed explicitly rather than relied on implicitly.
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
    status = pisa.CreatePDF(html, dest=buffer)
    if status.err:
        raise ValueError(f"PDF generation failed for invoice {invoice.id}")
    return buffer.getvalue()
