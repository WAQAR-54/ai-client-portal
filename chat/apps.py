import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class ChatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chat"

    def ready(self):
        # Startup diagnostic (masked prefix only, never the full key) so a
        # misloaded key (e.g. a stale/empty OPENAI_API_KEY sitting in the
        # launching shell's own environment silently beating .env — the
        # actual root cause of a real incident) shows up in the log the
        # moment the server starts, instead of only surfacing as a vague
        # "assistant hit a problem" during live use. Runs from here (not
        # settings.py) because Django's LOGGING config isn't applied yet
        # while settings.py itself is still executing.
        from django.conf import settings

        openai_key = settings.OPENAI_API_KEY
        anthropic_key = settings.ANTHROPIC_API_KEY
        logger.info(
            "Startup: OPENAI_API_KEY=%s (len=%d), ANTHROPIC_API_KEY=%s (len=%d)",
            (openai_key[:10] + "...") if openai_key else "<empty>",
            len(openai_key),
            (anthropic_key[:10] + "...") if anthropic_key else "<empty>",
            len(anthropic_key),
        )
