"""Every invoice-related outbound email goes through this module - kept
separate from billing/views.py so billing/tasks.py's scheduled overdue-
reminder sweep can build and send the exact same email a human "Email to
client" click sends, without importing views (which pulls in the whole
request-handling stack). One implementation of "build this invoice's
email" per kind, used by both the manual action and the automation.
"""

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from billing.pdf import render_invoice_pdf
from governance.models import SiteBranding
from notifications.emailing import send_tracked_email


def share_url_for_invoice(invoice):
    """Absolute URL for the no-login public invoice view - built from
    settings.SITE_URL (not request.build_absolute_uri(), which depends on
    the request's Host header passing ALLOWED_HOSTS validation, a real
    failure mode in production that this sidesteps entirely) so it works
    equally from a request-driven view or a request-less Celery task."""
    return settings.SITE_URL.rstrip("/") + reverse("billing:public_invoice", kwargs={"token": invoice.share_token})


def _logo_url(site_branding):
    if not site_branding.logo:
        return None
    return settings.SITE_URL.rstrip("/") + site_branding.logo.url


def _pdf_attachment(invoice):
    return (f"{invoice.invoice_number}.pdf", render_invoice_pdf(invoice), "application/pdf")


def send_invoice_email(invoice):
    """The "here's your invoice" email - billing.views.email_invoice_to_client's
    manual Send action. Never raises - returns (success: bool, error: str | None),
    same contract as notifications.emailing.send_tracked_email."""
    if invoice.recipient_user_id is None or not invoice.recipient_user.email:
        return False, "This invoice has no recipient email to send to."

    site_branding = SiteBranding.load()
    share_url = share_url_for_invoice(invoice)
    subject = f"{site_branding.site_name}: Invoice {invoice.invoice_number}"
    text_body = (
        f"Your invoice {invoice.invoice_number} ({invoice.currency} {invoice.total}), due {invoice.due_date}, "
        f"is attached as a PDF.\n\nView it online (no login needed): {share_url}"
    )
    html_body = render_to_string(
        "billing/email_invoice.html",
        {
            "invoice": invoice,
            "site_branding": site_branding,
            "logo_url": _logo_url(site_branding),
            "share_url": share_url,
        },
    )
    return send_tracked_email(
        invoice.recipient_user.email, subject, text_body, html_body=html_body, attachments=[_pdf_attachment(invoice)]
    )


def send_overdue_reminder_email(invoice):
    """The dunning nudge - billing.tasks.send_overdue_reminders, sent at
    most once per invoice (see Invoice.reminder_sent_at)."""
    if invoice.recipient_user_id is None or not invoice.recipient_user.email:
        return False, "This invoice has no recipient email to send to."

    site_branding = SiteBranding.load()
    share_url = share_url_for_invoice(invoice)
    days_overdue = (timezone.localdate() - invoice.due_date).days
    subject = f"{site_branding.site_name}: Invoice {invoice.invoice_number} is overdue"
    text_body = (
        f"Your invoice {invoice.invoice_number} ({invoice.currency} {invoice.total}) was due {invoice.due_date} "
        f"and is now {days_overdue} day(s) overdue.\n\nPay or view it online: {share_url}"
    )
    html_body = render_to_string(
        "billing/email_overdue_reminder.html",
        {
            "invoice": invoice,
            "site_branding": site_branding,
            "logo_url": _logo_url(site_branding),
            "share_url": share_url,
            "days_overdue": days_overdue,
        },
    )
    return send_tracked_email(
        invoice.recipient_user.email, subject, text_body, html_body=html_body, attachments=[_pdf_attachment(invoice)]
    )
