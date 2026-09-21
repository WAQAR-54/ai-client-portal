from django.core.management.base import BaseCommand

from accounts.models import User
from governance.branding import brand_name
from notifications.emailing import send_tracked_email

# RFC 2606 names that can never receive mail. A placeholder account on one of them (the demo "admin@example.com")
# would otherwise be sent to - and bounce with a 550 - on every deploy.
RESERVED_DOMAINS = ("example.com", "example.org", "example.net")
RESERVED_SUFFIXES = (".example", ".test", ".invalid", ".localhost")


def can_receive_mail(email):
    domain = email.rsplit("@", 1)[-1].lower().rstrip(".")
    if domain in RESERVED_DOMAINS or any(domain.endswith("." + d) for d in RESERVED_DOMAINS):
        return False
    return not domain.endswith(RESERVED_SUFFIXES)


class Command(BaseCommand):
    """Called from the deploy job's SSH steps in .github/workflows/ci.yml -
    once after the health check passes, and once (with the failure/rollback
    context) if it doesn't - so a SuperAdmin finds out a deploy happened
    without having to go check GitHub Actions. Reuses send_tracked_email
    (the same path every other email in the app goes through) rather than
    needing separate SMTP secrets in GitHub Actions - whatever's already
    configured on Email Logs > Settings, or the EMAIL_* env fallback, just
    works here too."""

    help = "Emails every active SuperAdmin about the outcome of a production deploy."

    def add_arguments(self, parser):
        parser.add_argument("--status", required=True, choices=["success", "failure"])
        parser.add_argument("--sha", required=True)
        parser.add_argument("--prev-sha", default="")

    def handle(self, *args, **options):
        sha = options["sha"][:12]
        prev_sha = options["prev_sha"][:12] if options["prev_sha"] else None

        everyone = list(User.objects.filter(role=User.Role.SUPERADMIN, is_active=True).values_list("email", flat=True))
        recipients = [email for email in everyone if can_receive_mail(email)]
        skipped = len(everyone) - len(recipients)
        if not recipients:
            self.stdout.write("No active SuperAdmin with a real email address to notify - skipping.")
            return

        if options["status"] == "success":
            subject = f"[{brand_name()}] Deploy succeeded ({sha})"
            body = f"Commit {sha} was deployed and passed the post-deploy health check. Live now."
        else:
            subject = f"[{brand_name()}] Deploy FAILED - rolled back ({sha})"
            body = (
                f"Commit {sha} failed its health check after deploy and was automatically rolled back"
                f"{f' to {prev_sha}' if prev_sha else ''}. The site is back on the previous working "
                "version. Check the GitHub Actions run for what went wrong before pushing again."
            )

        sent = 0
        for email in recipients:
            ok, _error = send_tracked_email(email, subject, body)
            sent += ok
        self.stdout.write(f"Notified {sent}/{len(recipients)} SuperAdmin(s).")
        # A GitHub annotation, so the outcome is visible on the run page (the step itself never fails a deploy).
        # "Accepted" is what the mail server told us; whether it lands in an inbox or in Spam is not knowable here.
        note = f" ({skipped} placeholder address(es) skipped)" if skipped else ""
        if sent == len(recipients):
            self.stdout.write(
                f"::notice title=Deploy email::Accepted by the mail server for {sent} SuperAdmin(s){note}. "
                "If it is not in the inbox, check Spam."
            )
        else:
            self.stdout.write(
                f"::warning title=Deploy email::{len(recipients) - sent} of {len(recipients)} SuperAdmin(s) did NOT "
                f"get the deploy email (mail server refused it){note}. See Email Logs for the reason."
            )
