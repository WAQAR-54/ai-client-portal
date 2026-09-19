"""AsyncAdminEmailHandler - see config/settings.py's LOGGING dict (the
"mail_admins" handler wired to the "django.request" logger). Subclasses
Django's own AdminEmailHandler purely to swap its final send step: the
built-in version calls django.core.mail.mail_admins() synchronously, which
would block the request thread (or a Gunicorn worker) on SMTP for every
single unhandled exception in production. Everything before that - building
the subject line, running the traceback through Django's own
SafeExceptionReporterFilter to redact settings/session values that look
like secrets/passwords/tokens - is inherited unchanged from AdminEmailHandler.
"""

import logging

from django.conf import settings
from django.utils.log import AdminEmailHandler


class AsyncAdminEmailHandler(AdminEmailHandler):
    def send_mail(self, subject, message, *args, **kwargs):
        if not settings.ADMINS:
            return
        from notifications.tasks import send_admin_error_alert

        send_admin_error_alert.delay(subject, message)


class HealthProbeDowngradeFilter(logging.Filter):
    """Keeps the health probes from paging anyone.

    Django logs every 5xx *response* (not only exceptions) on "django.request"
    at ERROR, and that logger feeds the email handlers (and Sentry's
    ERROR-level capture). /healthz/ and /healthz/deep/ are polled every few
    seconds by Docker, the CI deploy gate and uptime monitors, and they
    answer 503 by design when a dependency is down - so without this every
    poll during an outage would send an alert email.

    Only that exact case is downgraded to WARNING (still written to the
    console/file log): a 503, from one of the two probe paths, that came back
    as a normal response. An exception raised inside a probe carries exc_info
    and stays an ERROR, and a 503 from any other URL is untouched.
    """

    PATHS = frozenset({"/healthz/", "/healthz/deep/"})

    def filter(self, record):
        request = getattr(record, "request", None)
        if (
            record.levelno >= logging.ERROR
            and getattr(record, "status_code", None) == 503
            and not record.exc_info
            and getattr(request, "path", None) in self.PATHS
        ):
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True
