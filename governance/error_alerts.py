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

from django.conf import settings
from django.utils.log import AdminEmailHandler


class AsyncAdminEmailHandler(AdminEmailHandler):
    def send_mail(self, subject, message, *args, **kwargs):
        if not settings.ADMINS:
            return
        from notifications.tasks import send_admin_error_alert

        send_admin_error_alert.delay(subject, message)
