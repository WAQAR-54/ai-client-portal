"""Every real email the app sends goes through send_via_connection (either
via send_tracked_email, using the saved EmailSettings row, or directly
from governance/views.py::send_test_email, which builds its own connection
from whatever the admin currently has typed in the Email Configuration
modal - deliberately NOT the saved settings, since the whole point of a
test button is to check unsaved changes before committing them). Both
paths always log to EmailLog, so the admin Email Logs page shows every
attempt either way."""

import logging

from django.core.mail import EmailMultiAlternatives, get_connection
from django.urls import reverse

logger = logging.getLogger(__name__)


def build_connection(host, port, username, password, encryption):
    return get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=host,
        port=port,
        username=username,
        password=password,
        use_tls=encryption == "tls",
        use_ssl=encryption == "ssl",
        fail_silently=False,
    )


def _tracking_pixel_html(tracking_token):
    from django.conf import settings

    url = settings.SITE_URL.rstrip("/") + reverse("notifications:track_email_open", kwargs={"token": tracking_token})
    return f'<img src="{url}" width="1" height="1" alt="" style="display:none;">'


def send_via_connection(connection, from_email, to_email, subject, text_body, html_body=None, attachments=None):
    """Sends one email over an already-built connection, always logging an
    EmailLog row first so a send that raises mid-flight still leaves a
    FAILED row behind rather than no record at all. Never raises - returns
    (success: bool, error_message: str | None).

    `attachments` is a list of (filename, content_bytes, mimetype) tuples -
    e.g. billing.views.email_invoice_to_client attaching the actual
    invoice PDF, not just a link to it."""
    from notifications.models import EmailLog

    log = EmailLog.objects.create(recipient=to_email, subject=subject, status=EmailLog.Status.FAILED)
    try:
        message = EmailMultiAlternatives(
            subject=subject, body=text_body, from_email=from_email, to=[to_email], connection=connection
        )
        if html_body:
            message.attach_alternative(html_body + _tracking_pixel_html(log.tracking_token), "text/html")
        for filename, content, mimetype in attachments or []:
            message.attach(filename, content, mimetype)
        message.send()
    except Exception as exc:  # noqa: BLE001 - any SMTP/connection failure is a normal, reportable outcome here
        log.error_message = str(exc)
        log.save(update_fields=["error_message"])
        logger.warning("Email send failed to %s: %s", to_email, exc)
        return False, str(exc)

    log.status = EmailLog.Status.SENT
    log.save(update_fields=["status"])
    return True, None


def send_tracked_email(to_email, subject, text_body, html_body=None, attachments=None):
    """Sends using the saved EmailSettings row (the normal, non-test case -
    see notifications/tasks.py::send_notification_email). Falls back to
    Django's own EMAIL_BACKEND/settings.py EMAIL_* config (the console
    backend in local dev, or an already-set-up env-var SMTP config in an
    existing deployment) when EmailSettings hasn't been configured via the
    admin UI yet - this feature must not silently stop notification
    emails from sending on any environment that already had EMAIL_HOST
    set before EmailSettings existed."""
    from django.conf import settings as django_settings

    from notifications.models import EmailSettings

    settings_row = EmailSettings.load()
    if settings_row.is_configured():
        connection = build_connection(
            settings_row.host,
            settings_row.port,
            settings_row.username,
            settings_row.get_password(),
            settings_row.encryption,
        )
        from_email = settings_row.from_address or settings_row.username
    else:
        connection = get_connection(fail_silently=False)
        from_email = django_settings.DEFAULT_FROM_EMAIL
    return send_via_connection(connection, from_email, to_email, subject, text_body, html_body, attachments)
