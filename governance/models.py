from django.conf import settings
from django.db import models

from accounts.models import Department


class SystemPromptVersionManager(models.Manager):
    def create_new_version(self, department, content, created_by=None, tone_preference=None, restricted_topics=""):
        """The only correct way to add a version: atomically deactivates
        whatever was active for this department first, so at most one
        version per department is ever active."""
        from django.db import transaction

        with transaction.atomic():
            self.filter(department=department, is_active=True).update(is_active=False)
            return self.create(
                department=department,
                content=content,
                tone_preference=tone_preference or SystemPromptVersion.Tone.FORMAL,
                restricted_topics=restricted_topics,
                created_by=created_by,
                is_active=True,
            )


class SystemPromptVersion(models.Model):
    class Tone(models.TextChoices):
        FORMAL = "formal", "Formal"
        CASUAL = "casual", "Casual"
        TECHNICAL = "technical", "Technical"

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="system_prompt_versions")
    content = models.TextField(help_text="Company/department context injected into the base system prompt.")
    tone_preference = models.CharField(max_length=20, choices=Tone.choices, default=Tone.FORMAL)
    restricted_topics = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=False)

    objects = SystemPromptVersionManager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.department.name} v{self.pk} ({'active' if self.is_active else 'archived'})"


class UsageLimit(models.Model):
    """Applies to a single user, or to an entire department if user is null.
    A user-level limit overrides the department-level one for that user."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="usage_limit",
    )
    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, null=True, blank=True, related_name="usage_limits",
    )
    daily_token_cap = models.PositiveIntegerField(null=True, blank=True)
    monthly_token_cap = models.PositiveIntegerField(null=True, blank=True)
    session_limit = models.PositiveIntegerField(
        null=True, blank=True, help_text="Max user messages allowed in a single conversation.",
    )
    budget_cap_currency = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, help_text="Monthly spend cap in USD.",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(user__isnull=False, department__isnull=True)
                    | models.Q(user__isnull=True, department__isnull=False)
                ),
                name="usage_limit_exactly_one_of_user_or_department",
            ),
        ]

    def __str__(self):
        target = self.user or self.department
        return f"UsageLimit({target})"


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    action_type = models.CharField(max_length=100)
    target_type = models.CharField(max_length=100)
    target_id = models.CharField(max_length=100, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.actor} {self.action_type} {self.target_type}:{self.target_id}"
