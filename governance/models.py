from django.conf import settings
from django.db import models

from accounts.models import Department

KNOWN_FEATURE_FLAGS = [
    ("file_upload", "File upload in chat"),
    ("export", "Conversation export"),
    ("priority_routing", "Priority model routing"),
    # Added for the 4-dimension Plan restructure. "tools" and "priority_queue"
    # are stored/editable here but have no enforcement point anywhere in the
    # app yet - no function/tool-calling capability or request-priority queue
    # exists to gate. "long_context" is enforced only indirectly: it's not
    # independently checked, since a plan's own max_context_tokens value
    # already controls its actual context ceiling (see governance/plans.py's
    # validate_context_tokens) - this flag is descriptive of that, not a
    # separate gate. Documented here rather than silently wired to nothing.
    ("tools", "Tool/function calling"),
    ("priority_queue", "Priority request queue"),
    ("long_context", "Long-context requests"),
    # Unlike the flags above (informational only, no enforcement point yet),
    # this one IS enforced - see governance/plans.py::has_feature and its use
    # in chat/views.py::chat_home. Existing plans were backfilled to True by
    # migration 0010 so this addition doesn't silently take the dropdown away
    # from anyone already relying on it; a newly created plan defaults to
    # unchecked/off like every other flag in this list, requiring an explicit
    # opt-in.
    ("model_selection", "Manual model selection"),
]

# ---------- Role-wide feature visibility ----------
# Distinct from KNOWN_FEATURE_FLAGS above, which is a per-PLAN grant (what a
# given user's subscription includes). This is a per-ROLE switch a
# SuperAdmin controls directly ("hide this whole capability from every
# Admin", "hide this from every plain User") - independent of which Plan
# anyone is on. See RoleFeatureToggle below and governance/features.py for
# the enforcement helper.

# Admin nav sections a SuperAdmin can hide from the Admin role. Manager and
# User can never reach these regardless (blocked by role_required's
# hierarchy before a feature toggle is even checked), so these are only
# ever meaningful for the "admin" role.
ADMIN_NAV_FEATURES = [
    ("teams", "Teams"),
    ("upgrade_requests", "Upgrade Requests"),
    ("limits", "Limits"),
    ("usage_cost", "Usage & Cost"),
    ("audit_logs", "Audit Logs"),
    ("feedback", "Feedback"),
    ("department_settings", "Department Settings (system prompt / templates)"),
]

# Chat/Settings features any signed-in role (User, Manager, Admin) uses -
# independent of the per-plan KNOWN_FEATURE_FLAGS above. A SuperAdmin always
# has all of these; that's not a row here since it's not actually a choice.
USER_CHAT_FEATURES = [
    ("prompt_templates", "Prompt templates (save/insert in composer)"),
    ("quick_switcher", "Keyboard shortcuts / Ctrl+K quick-switcher"),
    ("conversation_pin_search", "Pin & search conversations"),
    ("dark_mode", "Dark mode toggle (Settings > Display)"),
    ("notifications", "Notifications (bell + email preferences)"),
    ("upgrade_request", '"Request upgrade" button/flow'),
    ("onboarding_tour", "Guided onboarding tour"),
    ("code_playground", "Code Playground (standalone, off by default - see its own migration)"),
    ("domain_generator", "Domain Generator (standalone, off by default - see its own migration)"),
]

ROLE_FEATURE_ROLES = ["user", "manager", "admin"]


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

    # Per-seat billing (billing app): how many accounts.User rows (seats) a
    # Department on this plan gets before paying extra per additional
    # person - see billing.models.RegionalPrice.extra_seat_price for what
    # that extra seat actually costs (region-specific, lives there since
    # it's a currency amount; this is just the plan-level included count).
    # Null = unlimited seats included, never billed for extras. Named
    # seats_included (not teams_included, its original name) - it counts
    # people in the department, not accounts.Team rows.
    seats_included = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=1,
        help_text="Seats (users) a department gets before being billed per extra person. Blank = unlimited.",
    )

    # Request-COUNT cap, independent of the token-volume caps above (a user
    # could send many short messages without tripping daily_token_limit, or
    # few very long ones without tripping this) - both are enforced.
    max_requests_per_period = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max messages allowed within one `period`. Null = no request-count cap.",
    )

    class Period(models.TextChoices):
        SESSION = "session", "Session"
        DAY = "day", "Day"
        MONTH = "month", "Month"

    period = models.CharField(
        max_length=10,
        choices=Period.choices,
        null=True,
        blank=True,
        help_text="Which window max_requests_per_period counts against.",
    )
    max_context_tokens = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Per-request cap on assembled prompt size (system prompt + history + attachments), "
            "distinct from daily_token_limit/monthly_token_limit which cap cumulative usage over time."
        ),
    )
    auto_upgrade_threshold_spend = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Reserved for future use (auto-suggest an upgrade past this spend). Not yet enforced anywhere.",
    )

    allowed_models = models.ManyToManyField("chat.ModelConfig", blank=True, related_name="plans")
    # Parallel field for the ModelConfig -> providers.ProviderModel
    # migration (expand-migrate-contract, step 1 of 3 - see providers/
    # management/commands/migrate_models_to_provider_model.py). Populated
    # alongside allowed_models by that command; chat/router.py and
    # chat/views.py still read allowed_models exclusively until step 3
    # cuts them over, so this field is inert (write-only from the
    # migration command's point of view) until then.
    allowed_provider_models = models.ManyToManyField("providers.ProviderModel", blank=True, related_name="plans_v2")

    # Budget automation: once a user's month-to-date spend against
    # monthly_budget_cap crosses auto_downgrade_threshold_pct, new replies
    # are forced onto auto_downgrade_fallback_model instead of blocking the
    # user outright - see governance/plans.py::get_budget_automation_status
    # and chat/views.py::stream_message. Meaningless without
    # monthly_budget_cap set (nothing to measure against), so the admin UI
    # (governance/views.py::BudgetAutomationView) disables the toggle for
    # any plan missing a budget cap rather than letting it be turned on.
    auto_downgrade_enabled = models.BooleanField(default=False)
    auto_downgrade_threshold_pct = models.PositiveSmallIntegerField(
        default=80,
        help_text="Percent of monthly_budget_cap spent before auto-downgrade kicks in.",
    )
    auto_downgrade_fallback_model = models.ForeignKey(
        "providers.ProviderModel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Model to force new replies onto once the threshold is crossed.",
    )

    feature_flags = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            'e.g. {"file_upload": true, "export": true, "tools": true, ' '"priority_queue": true, "long_context": true}'
        ),
    )

    # Capability Limits: numeric caps distinct from the request/token-volume
    # caps above - these bound specific actions rather than overall usage.
    # Null means "no cap" for the first two; for the standalone tools' caps
    # null means "use that tool's own global default" (playground.views.
    # DAILY_RUN_LIMIT / domaingen.views.DAILY_SEARCH_LIMIT), since those
    # already have a sensible baseline that shouldn't silently become
    # unlimited just because a plan doesn't override it.
    max_message_length = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max characters allowed in a single message. Null = no limit.",
    )
    max_compare_uses_per_day = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max Compare-mode (2-model) messages per day. Null = no limit.",
    )
    max_playground_runs_per_day = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Overrides Code Playground's global daily run limit for this plan. Null = use the global default.",
    )
    max_domain_searches_per_day = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Overrides Domain Generator's global daily search limit for this plan. Null = use the global default."
        ),
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


class RoleFeatureToggle(models.Model):
    """SuperAdmin-controlled, per-role visibility switch for a whole app
    capability (see ADMIN_NAV_FEATURES / USER_CHAT_FEATURES above) - not
    tied to any Plan. Absence of a row for a (role, feature_key) pair means
    "visible" (the default, so nothing silently disappears the moment this
    table is introduced) - only an explicit is_enabled=False row hides it,
    via governance/features.py's role_has_feature()."""

    role = models.CharField(max_length=20)
    feature_key = models.CharField(max_length=50)
    is_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["role", "feature_key"], name="unique_role_feature_toggle"),
        ]

    def __str__(self):
        return f"{self.role}:{self.feature_key} = {self.is_enabled}"


class RoutingRule(models.Model):
    """Admin-defined "if condition then model" override, checked before the
    tier-classification smart-routing already in chat/router.py (see
    chat/router.py::match_routing_rule) - runs only when the user didn't
    manually pick a model, same as tier classification. First active rule
    (by priority) whose condition matches AND whose target_model is one the
    requesting user can actually use wins; otherwise routing falls through
    unchanged to the existing tier-classification behavior."""

    class Condition(models.TextChoices):
        CODE_LIKE = "code_like", "Task looks like code"
        CASUAL_SHORT = "casual_short", "Casual / short question"
        IMAGE_ATTACHED = "image_attached", "Image attached"
        LONG_DOCUMENT_ATTACHED = "long_document_attached", "Long document attached"

    condition = models.CharField(max_length=30, choices=Condition.choices)
    target_model = models.ForeignKey("providers.ProviderModel", on_delete=models.CASCADE, related_name="routing_rules")
    priority = models.PositiveIntegerField(default=0, help_text="Lower runs first when more than one rule matches.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return f"{self.get_condition_display()} -> {self.target_model}"


class SecuritySettings(models.Model):
    """Singleton (always pk=1, via .load()) holding global security
    toggles managed from the Feature Visibility admin page - a real DB
    row rather than a Django setting/env var, specifically so a SuperAdmin
    can flip this without needing server/SSH access. mfa_required_for_admins
    gates accounts/mfa.py::user_requires_mfa's mandatory-for-Admin/
    SuperAdmin behavior - defaults to False since turning it on in an
    environment where outbound email isn't yet confirmed reliable could
    otherwise lock every admin out waiting on a code that never arrives."""

    mfa_required_for_admins = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Security settings"
        verbose_name_plural = "Security settings"

    def __str__(self):
        return "Security settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SiteBranding(models.Model):
    """Singleton (always pk=1, via .load()) holding the org's white-label
    identity - shown in the sidebar/topbar brand mark, the browser tab
    title/favicon, and the login page, via governance.context_processors.
    branding (registered on every request, so every template - including
    ones outside this app - can read `site_branding` without each view
    passing it explicitly). logo/favicon are optional: templates fall back
    to the plain accent dot / no favicon when unset, exactly like a fresh
    install today."""

    site_name = models.CharField(max_length=100, default="AI Client Portal")
    tagline = models.CharField(max_length=200, blank=True, default="")
    logo = models.ImageField(upload_to="branding/", null=True, blank=True)
    favicon = models.ImageField(upload_to="branding/", null=True, blank=True)

    class Meta:
        verbose_name = "Site branding"
        verbose_name_plural = "Site branding"

    def __str__(self):
        return "Site branding"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ComplianceSettings(models.Model):
    """Singleton (always pk=1, via .load()) holding org-wide Data Handling
    toggles that don't belong to any one Provider/Department - see
    chat/router.py's zero-retention filter and governance/pii.py's PII
    scanning gate."""

    only_zero_retention_models = models.BooleanField(default=False)
    pii_scanning_enabled = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Compliance settings"
        verbose_name_plural = "Compliance settings"

    def __str__(self):
        return "Compliance settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class PIIRule(models.Model):
    """One fixed, pre-seeded data type governance/pii.py scans outgoing
    messages for when ComplianceSettings.pii_scanning_enabled is on - see
    the seed migration. Not admin-addable/removable (a short fixed
    checklist, not an open rule set), just toggled and given an action."""

    class Kind(models.TextChoices):
        NATIONAL_ID = "national_id", "CNIC / National ID numbers"
        CREDIT_CARD = "credit_card", "Credit card numbers"
        PHONE_NUMBER = "phone_number", "Phone numbers"

    class Action(models.TextChoices):
        BLOCK = "block", "Block"
        REDACT = "redact", "Redact"
        WARN = "warn", "Warn only"

    kind = models.CharField(max_length=20, choices=Kind.choices, unique=True)
    action = models.CharField(max_length=10, choices=Action.choices, default=Action.WARN)
    is_enabled = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.get_kind_display()} ({self.get_action_display()})"
