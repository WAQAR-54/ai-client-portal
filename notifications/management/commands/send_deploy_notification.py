from django.core.management.base import BaseCommand

from accounts.models import User
from notifications.emailing import send_tracked_email


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

        recipients = list(
            User.objects.filter(role=User.Role.SUPERADMIN, is_active=True).values_list("email", flat=True)
        )
        if not recipients:
            self.stdout.write("No active SuperAdmin to notify - skipping.")
            return

        if options["status"] == "success":
            subject = f"Deploy succeeded ({sha})"
            body = f"Commit {sha} was deployed and passed the post-deploy health check. Live now."
        else:
            subject = f"Deploy FAILED - rolled back ({sha})"
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
