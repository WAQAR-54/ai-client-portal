import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Deliberately sends one test event to Sentry (a message, or a real "
        "raised-and-caught exception with --raise) so you can confirm it "
        "actually shows up in your Sentry project - not just that "
        "config/settings.py's sentry_sdk.init() block looks right on paper. "
        "Refuses to run if SENTRY_DSN isn't set (nothing would happen "
        "silently otherwise)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--raise",
            action="store_true",
            dest="raise_exception",
            help="Raise and catch a real exception instead of sending a plain message - "
            "exercises the same capture path an actual bug in production would.",
        )

    def handle(self, *args, **options):
        if not settings.SENTRY_DSN:
            raise CommandError(
                "SENTRY_DSN is not set - there's nothing to verify against. Set it in the "
                "server's own .env (see .env.example) and re-run this command there."
            )

        import sentry_sdk

        marker = uuid.uuid4().hex[:12]

        if options["raise_exception"]:
            try:
                raise RuntimeError(f"verify_sentry deliberate test error [{marker}]")
            except RuntimeError as exc:
                event_id = sentry_sdk.capture_exception(exc)
        else:
            event_id = sentry_sdk.capture_message(f"verify_sentry deliberate test message [{marker}]")

        # Sentry's SDK batches/sends events on a background thread - without
        # this, the process (and this command) could exit before the event
        # actually left the machine, especially for a short-lived management
        # command like this one rather than a long-running web/worker process.
        sentry_sdk.flush(timeout=5)

        self.stdout.write(
            self.style.SUCCESS(
                f"Sent test event {event_id} (marker: {marker}) to Sentry. Check your Sentry "
                f"project's Issues feed for '{marker}' - it can take a few seconds to appear."
            )
        )
