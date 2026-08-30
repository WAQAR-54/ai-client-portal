from django.conf import settings
from django.db import models

from accounts.models import Department

KNOWN_FEATURE_FLAGS = [
    ("file_upload", "File upload in chat"),
    ("export", "Conversation export"),
    ("priority_routing", "Priority model routing"),
]


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
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="usage_limit",
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="usage_limits",
    )
    daily_token_cap = models.PositiveIntegerField(null=True, blank=True)
    monthly_token_cap = models.PositiveIntegerField(null=True, blank=True)
    session_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max user messages allowed in a single conversation.",
    )
    budget_cap_currency = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Monthly spend cap in USD.",
    )
    max_upload_size_mb = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            f"File upload size cap in MB. Leave blank to use the system "
            f"default ({settings.DEFAULT_MAX_UPLOAD_SIZE_MB}MB)."
        ),
    )
    allowed_file_extensions = models.CharField(
        max_length=500,
        blank=True,
        help_text=(
            f"Comma-separated, no dots (e.g. pdf,png,txt). Leave blank to use "
            f"the system default ({settings.DEFAULT_ALLOWED_FILE_EXTENSIONS})."
        ),
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


class Plan(models.Model):
    """Bundles model access, limits, and feature flags into one assignable
    tier. Per-user UsageLimit/UserModelPermission rows (above) still exist
    as explicit overrides layered ON TOP of a user's Plan — see
    governance/plans.py for the precedence rules. Plans do not replace
    those tables; they replace "no plan at all" as the default baseline."""

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    is_demo = models.BooleanField(default=False, help_text="Marks this as a time-limited trial plan.")
    demo_duration_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Only used when is_demo is set — days until this plan expires for a user.",
    )

    daily_token_limit = models.PositiveIntegerField(null=True, blank=True)
    monthly_token_limit = models.PositiveIntegerField(null=True, blank=True)
    messages_per_session_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max user messages allowed in a single conversation.",
    )
    sessions_per_day_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max new conversations a user may start per day.",
    )
    monthly_budget_cap = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    allowed_models = models.ManyToManyField("chat.ModelConfig", blank=True, related_name="plans")
    feature_flags = models.JSONField(
        default=dict,
        blank=True,
        help_text='e.g. {"file_upload": true, "export": true}',
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Inactive plans can't be newly assigned, but existing history is kept.",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="The plan new users are auto-assigned. Only one plan should have this set.",
    )
    is_visible_to_admins = models.BooleanField(
        default=True,
        help_text="Whether this plan appears in the Change Plan picker, or stays hidden/archived.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def has_feature(self, flag_name):
        return bool(self.feature_flags.get(flag_name, False))


class UserPlanAssignment(models.Model):
    """A user's current Plan. One row per user (not a history log — plan
    CHANGES are recorded in AuditLog via governance.plans.assign_plan(),
    and `previous_plan` gives an at-a-glance look at what they moved from)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="plan_assignment",
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="assignments")
    previous_plan = models.ForeignKey(
        Plan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set automatically for demo plans; blank = never expires.",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Null if this was an automatic default assignment rather than an explicit admin action.",
    )

    def __str__(self):
        return f"{self.user} -> {self.plan}"


class UpgradeRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DISMISSED = "dismissed", "Dismissed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="upgrade_requests")
    current_plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, related_name="+")
    requested_plan = models.ForeignKey(
        Plan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Which plan the user asked to move to, if they specified one.",
    )
    message = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} wants an upgrade ({self.status})"


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
