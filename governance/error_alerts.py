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


def alert_recipients():
    """Who a crash alert goes to: the addresses in ADMINS when it is set; otherwise every active SuperAdmin
    (the same people the deploy notification already emails), so an unset ADMINS no longer means nobody is told.
    Never raises: this runs while something else has already gone wrong, and the database may be the problem."""
    if settings.ADMINS:
        return [email for _name, email in settings.ADMINS]
    try:
        from accounts.models import User

        return list(User.objects.filter(role=User.Role.SUPERADMIN, is_active=True).values_list("email", flat=True))
    except Exception:  # noqa: BLE001
        return []


class AsyncAdminEmailHandler(AdminEmailHandler):
    """The ONE admin-alert path (config/settings.py::LOGGING also removes
    Django's stock synchronous AdminEmailHandler from the "django" logger -
    with both attached, every unhandled 500 emailed the admins twice)."""

    def send_mail(self, subject, message, *args, **kwargs):
        if not alert_recipients():
            return
        from notifications.tasks import send_admin_error_alert

        # Django's own mail_admins() adds EMAIL_SUBJECT_PREFIX; keep it so inbox
        # rules that match "[Django]" keep working.
        prefixed = f"{settings.EMAIL_SUBJECT_PREFIX}{subject}"
        try:
            send_admin_error_alert.delay(prefixed, message)
        except Exception:
            # The broker (Redis) being down is exactly when errors are likely, and it
            # is what the deleted synchronous handler used to cover. Fall back to a
            # direct send so the alert is not lost - still one email, because the
            # queued path raised before anything was sent. A logging handler must
            # never raise into the code that logged, so this is best-effort.
            try:
                super().send_mail(subject, message, *args, **kwargs)
            except Exception:  # noqa: BLE001
                pass


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
