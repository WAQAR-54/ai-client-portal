from django.conf import settings
from django.db import models


class PlaygroundRun(models.Model):
    """One "Run" click in the Code Playground. Execution itself is entirely
    client-side/simulated (no real sandbox - see governance decision to
    ship UI-only first), but every click still logs a real row here so the
    daily quota and the admin Dashboard's usage stats are both genuine,
    not decorative."""

    class Language(models.TextChoices):
        PYTHON = "python", "Python 3.11"
        NODE = "node", "Node.js 20"
        BASH = "bash", "Bash"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="playground_runs")
    language = models.CharField(max_length=10, choices=Language.choices, default=Language.PYTHON)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user}: {self.get_language_display()} run at {self.created_at}"
