import logging

from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import translation
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


@shared_task(autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=600, max_retries=3)
def run_scheduled_database_backup():
    """Celery Beat wrapper around `manage.py backup_database` (see
    accounts/management/commands/backup_database.py and
    docs/BACKUP_RESTORE.md) - moves the recurring backup schedule onto
    this app's own Celery Beat infrastructure (already running the 5
    other daily/periodic sweeps) instead of depending on someone having
    separately configured a VPS crontab entry, which is easy to forget
    and gives no visible signal if it was never actually set up.

    The command itself already no-ops safely on SQLite and raises
    CommandError with a clear message when BACKUP_S3_BUCKET isn't set
    yet - both are expected, pre-configuration states, so they're caught
    and logged as a warning below (never retried - retrying a
    still-unconfigured bucket immediately would just fail identically).
    autoretry_for handles the OTHER case this didn't cover before: a
    genuinely transient failure mid-dump/upload (S3 network blip, pg_dump
    briefly unable to connect) used to fail the whole night's backup with
    no retry at all - now retried up to 3 times with backoff first."""
    try:
        call_command("backup_database")
    except CommandError as exc:
        logger.warning("Scheduled database backup did not run: %s", exc)


@shared_task(autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=30, max_retries=3)
def send_mfa_code_email_task(user_id, code):
    """Sends the login verification code by email off the request path -
    matches every other real email in the app going through Celery
    (notifications/notify.py -> send_notification_email.delay()) rather
    than blocking a login request on an SMTP round-trip. `user_id`/`code`
    only (not the User object) - plain-JSON Celery task args. Retries up
    to 3 times with a short (<=30s) backoff on any unexpected failure -
    capped short because the code itself expires in OTP_EXPIRY_MINUTES;
    note send_tracked_email itself already fails open (returns a status
    tuple, never raises) on an actual SMTP error, so this retry only
    ever fires for something else going wrong (e.g. a DB blip on the
    User lookup)."""
    from accounts.mfa import OTP_EXPIRY_MINUTES
    from accounts.models import User
    from notifications.emailing import send_tracked_email

    user = User.objects.filter(id=user_id).first()
    if not user or not user.email:
        return
    with translation.override(user.preferred_language):
        html_body = render_to_string(
            "accounts/email_mfa_code.html", {"code": code, "expiry_minutes": OTP_EXPIRY_MINUTES}
        )
    send_tracked_email(
        to_email=user.email,
        subject="[AI Client Portal] Your verification code",
        text_body=strip_tags(html_body),
        html_body=html_body,
    )


@shared_task(autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=120, max_retries=3)
def send_password_reset_email_task(user_id, uidb64, token):
    """Sends the "reset your password" email off the request path - same
    reasoning as send_mfa_code_email_task above (including the retry).
    The reset link itself stays valid far longer than an MFA code, so
    this can afford a slightly longer backoff cap. Builds the reset link
    from settings.SITE_URL rather than request.build_absolute_uri()
    (there's no request in a background task) - the same substitution
    notifications/tasks.py's own site_url already uses for email links
    sent from Celery. uidb64/token are computed synchronously at the
    call site (accounts/views.py::_send_password_reset_email) since
    that's cheap, deterministic, and needs no network I/O - only the
    actual send is deferred."""
    from accounts.models import User
    from notifications.emailing import send_tracked_email

    user = User.objects.filter(id=user_id).first()
    if not user or not user.email:
        return
    reset_url = settings.SITE_URL.rstrip("/") + reverse(
        "accounts:password_reset_confirm", kwargs={"uidb64": uidb64, "token": token}
    )
    with translation.override(user.preferred_language):
        html_body = render_to_string("accounts/email_password_reset.html", {"user": user, "reset_url": reset_url})
    send_tracked_email(
        to_email=user.email,
        subject="[AI Client Portal] Reset your password",
        text_body=strip_tags(html_body),
        html_body=html_body,
    )
