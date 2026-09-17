import secrets
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class ModelConfig(models.Model):
    class Provider(models.TextChoices):
        OPENAI = "openai", "OpenAI"
        ANTHROPIC = "anthropic", "Anthropic"

    class Tier(models.TextChoices):
        ECONOMY = "economy", _("Economy")
        DEFAULT = "default", _("Default")
        PREMIUM = "premium", _("Premium")

    provider = models.CharField(max_length=20, choices=Provider.choices)
    model_name = models.CharField(
        max_length=100,
        help_text="Exact provider API model identifier, e.g. gpt-4o-mini or claude-sonnet-4-5.",
    )
    display_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Shown to users instead of the raw model ID. Leave blank to just show the ID.",
    )
    tier = models.CharField(max_length=20, choices=Tier.choices, default=Tier.DEFAULT)
    input_cost_per_1m = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="USD per 1M input tokens. Verify against provider pricing before setting.",
    )
    output_cost_per_1m = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="USD per 1M output tokens. Verify against provider pricing before setting.",
    )
    is_enabled = models.BooleanField(
        default=False,
        help_text="Model is unusable until an admin verifies pricing and enables it here.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["provider", "model_name"]]
        ordering = ["provider", "tier", "model_name"]

    def __str__(self):
        return f"{self.get_provider_display()} / {self.model_name} ({self.tier})"

    @property
    def display_label(self):
        return self.display_name or self.model_name

    def estimate_cost(self, input_tokens, output_tokens):
        if self.input_cost_per_1m is None or self.output_cost_per_1m is None:
            return None
        return (
            Decimal(input_tokens) * self.input_cost_per_1m + Decimal(output_tokens) * self.output_cost_per_1m
        ) / Decimal(1_000_000)


class UserModelPermission(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="model_permissions")
    # Legacy ModelConfig-based grant/deny (SuperAdmin's org-wide "who can
    # use" screen on the Models (legacy) page). Nullable so a row can
    # instead target provider_model below - exactly one of the two is ever
    # set, enforced by the constraint below (same "exactly one of" pattern
    # as PromptTemplate's owner/department).
    model_config = models.ForeignKey(
        ModelConfig, on_delete=models.CASCADE, null=True, blank=True, related_name="user_permissions"
    )
    # New Provider-system grant/deny - what a Manager's per-team-member
    # model assignment (governance:toggle_member_model_permission) writes.
    # Only ever targets a ProviderModel the admin has explicitly marked
    # is_manager_assignable=True (see providers/models.py) - a Manager can
    # never grant a model the admin hasn't opted into that pool.
    provider_model = models.ForeignKey(
        "providers.ProviderModel", on_delete=models.CASCADE, null=True, blank=True, related_name="user_permissions"
    )
    is_allowed = models.BooleanField(default=True)

    class Meta:
        unique_together = [["user", "model_config"], ["user", "provider_model"]]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(model_config__isnull=False, provider_model__isnull=True)
                    | models.Q(model_config__isnull=True, provider_model__isnull=False)
                ),
                name="user_model_permission_exactly_one_of_model_config_or_provider_model",
            ),
        ]

    def __str__(self):
        target = self.model_config or self.provider_model
        return f"{self.user} -> {target} ({'allowed' if self.is_allowed else 'denied'})"

    @property
    def model_label(self):
        """Whichever of the two target fields is actually set - templates
        should read this instead of model_config.display_label directly, or
        the Model column silently goes blank for every provider_model-based
        row (see governance/_user_overrides.html)."""
        if self.provider_model_id:
            return f"{self.provider_model.display_label} ({self.provider_model.provider.name})"
        if self.model_config_id:
            return self.model_config.display_label
        return None


class ActiveConversationManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class Conversation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations")
    title = models.CharField(max_length=200, default="New conversation")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_pinned = models.BooleanField(default=False)
    pinned_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    # Denormalized from the most recent assistant reply's
    # Message.provider_model_used - written in chat/views.py::stream_message
    # at both completion points, not computed from conversation.messages on
    # every sidebar render (would be an extra query per conversation there).
    # Used only for the sidebar's provider filter tabs; never affects which
    # model the NEXT message in this conversation actually uses.
    last_provider_model = models.ForeignKey(
        "providers.ProviderModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Personal grouping (see Project below) - optional by design, same
    # "assigning one is optional" philosophy as accounts.User.department.
    # SET_NULL rather than CASCADE: deleting a Project must never delete
    # the conversations that were in it.
    project = models.ForeignKey(
        "Project", on_delete=models.SET_NULL, null=True, blank=True, related_name="conversations"
    )

    objects = ActiveConversationManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class Project(models.Model):
    """Personal, per-user grouping of Conversations (like Claude.ai's
    Projects) - deliberately NOT shared across a department/team, unlike
    most other org-structure concepts in this app (Team, Department).
    Gated by the "projects" USER_CHAT_FEATURES toggle (governance/models.py),
    not a Plan feature flag - it costs no provider spend, same category as
    conversation_pin_search."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_projects")
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=Role.choices)
    content = models.TextField(blank=True)
    model_used = models.ForeignKey(
        ModelConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
    )
    # Parallel field for the ModelConfig -> providers.ProviderModel
    # migration (expand-migrate-contract, step 1 of 3 - see providers/
    # management/commands/migrate_models_to_provider_model.py). Populated
    # alongside model_used by that command; chat/router.py and
    # chat/views.py still read/write model_used exclusively until step 3
    # cuts them over, so this field is inert (write-only from the
    # migration command's point of view) until then.
    provider_model_used = models.ForeignKey(
        "providers.ProviderModel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages_v2",
    )
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    attachment = models.FileField(upload_to="chat_attachments/%Y/%m/", null=True, blank=True)
    attachment_original_name = models.CharField(max_length=255, blank=True)
    attachment_size = models.PositiveIntegerField(null=True, blank=True, help_text="Bytes.")
    # Set once at creation - for a USER message's upload, from the
    # extension (chat/views.py, see chat/document_extraction.py::
    # IMAGE_EXTENSIONS); for an ASSISTANT message's Grok-generated media
    # (chat/media_generation.py), directly to "image"/"video" since there's
    # no upload extension to read. Blank when there's no attachment at
    # all. Denormalized purely so governance/limits.py::
    # check_attachment_monthly_limit/check_media_generation_monthly_limit
    # can count "how many of this kind this month" with a plain DB filter.
    attachment_kind = models.CharField(
        max_length=10, blank=True, choices=[("image", "Image"), ("document", "Document"), ("video", "Video")]
    )
    # Set on the pending assistant reply when the user explicitly turned on
    # Research mode for that send (chat/views.py::post_message) - lets
    # governance/limits.py::check_research_monthly_limit count "how many
    # Research uses this month" with a plain DB filter, the same
    # denormalize-for-counting reasoning as attachment_kind above.
    used_research = models.BooleanField(default=False)
    # Set on the pending assistant reply when the user explicitly turned on
    # the composer's "Generate document" toggle for that send
    # (chat/views.py::post_message) - a DIFFERENT, newer feature from the
    # existing generate_media(media_mode="document") one-shot flow (that one
    # never touches these fields; see chat/views.py's own note on the
    # naming collision). When True, _message_bubble.html renders a compact
    # doc card (opens the artifact side panel on click) instead of the
    # full inline markdown text. artifact_title starts as a provisional
    # truncated-prompt guess and is overwritten in stream_message once the
    # reply's own "# Heading" is known.
    is_artifact = models.BooleanField(default=False)
    artifact_title = models.CharField(max_length=200, blank=True)
    served_from_cache = models.BooleanField(
        default=False,
        help_text="This reply was served from the Redis response cache instead of "
        "calling the provider again - see chat/response_cache.py. tokens/estimated_cost "
        "still reflect what the (skipped) call would have cost, for usage-limit accounting; "
        "this flag is what the admin cost-saved metric sums over.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # An unguessable per-message credential required by chat:stream_message
    # (see chat/views.py) alongside the existing ownership check - closes
    # the narrow gap flagged early in this project: that endpoint is GET-
    # based (SSE requires it) and therefore CSRF-exempt by design, so
    # ownership alone left a real ID (predictable, sequential) as the only
    # thing standing between "this is my own pending message" and "I
    # guessed someone else's". Generated for every Message, not just
    # pending assistant ones, so nothing has to special-case which rows
    # need it - same reasoning as billing.models.Invoice.share_token.
    stream_token = models.CharField(max_length=48, unique=True, null=True, blank=True, editable=False)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"

    def save(self, *args, **kwargs):
        if not self.stream_token:
            self.stream_token = secrets.token_urlsafe(24)
        super().save(*args, **kwargs)

    @property
    def model_label(self):
        """Same "whichever of the two model-snapshot fields is actually
        set" pattern as MessageFeedback.model_label below - provider_model_used
        for every message since the ModelConfig -> ProviderModel cutover,
        model_used for everything before it. Used for the chat model badge
        and the "switched to X" divider."""
        if self.provider_model_used_id:
            return self.provider_model_used.display_label
        if self.model_used_id:
            return self.model_used.display_label
        return None

    @property
    def provider_slug(self):
        """CSS-token-friendly provider identifier for the model badge
        (.model-badge-{slug} in main.css) - providers.Provider.slug for a
        provider_model_used message, or the legacy ModelConfig.provider
        CharField value for an old one (already "openai"/"anthropic",
        identical to those two providers' real slugs, so no mapping is
        needed)."""
        if self.provider_model_used_id:
            return self.provider_model_used.provider.slug
        if self.model_used_id:
            return self.model_used.provider
        return ""

    @property
    def provider_color(self):
        """Provider.accent_color() for this message's provider, or None for
        a legacy model_used-only message (ModelConfig has no Provider row to
        read a color from) - callers fall back to the static per-slug CSS
        classes in that case, same as provider_slug's own legacy branch."""
        if self.provider_model_used_id:
            return self.provider_model_used.provider.accent_color()
        return None


class ArenaComparison(models.Model):
    """Compare mode: one user prompt answered by two models at once,
    shown side by side (see chat/views.py::post_arena_message). response_a
    and response_b are ordinary pending Message rows - stream_message
    (chat/views.py) fills each in exactly like a normal reply, just with an
    explicit model_id forcing which model each one uses, so no separate
    streaming code path exists for Compare mode. Kept as its own row (not a
    field on Message) so a comparison always has exactly the two responses
    it was created with, and so `picked` can point at either one without an
    awkward self-referential FK on Message itself."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="arena_comparisons")
    user_message = models.OneToOneField(Message, on_delete=models.CASCADE, related_name="arena_comparison")
    response_a = models.OneToOneField(Message, on_delete=models.CASCADE, related_name="arena_as_a")
    response_b = models.OneToOneField(Message, on_delete=models.CASCADE, related_name="arena_as_b")
    model_a = models.ForeignKey("providers.ProviderModel", on_delete=models.CASCADE, related_name="+")
    model_b = models.ForeignKey("providers.ProviderModel", on_delete=models.CASCADE, related_name="+")
    # Which of response_a/response_b the user judged better - null until
    # they click "Better" on one. Deliberately not exclusive of "neither":
    # a user who never picks just leaves this null.
    picked = models.ForeignKey(Message, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Arena #{self.pk}: {self.model_a} vs {self.model_b}"


class MessageFeedback(models.Model):
    class Rating(models.TextChoices):
        UP = "up", "Thumbs up"
        DOWN = "down", "Thumbs down"

    message = models.OneToOneField(Message, on_delete=models.CASCADE, related_name="feedback")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="message_feedback")
    rating = models.CharField(max_length=10, choices=Rating.choices)
    comment = models.TextField(blank=True)
    # Denormalized snapshot of message.model_used at rating time, so a
    # later model rename/removal doesn't erase which model this feedback
    # was actually about.
    model_used = models.ForeignKey(ModelConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Step-3 cutover counterpart of the field above - chat/views.py's
    # submit_feedback now snapshots message.provider_model_used here
    # instead (model_used stays for historical feedback rows already
    # written before this cutover, never populated for a new one going
    # forward - same "add a parallel field, never delete the old one"
    # pattern as Message.provider_model_used itself).
    provider_model_used = models.ForeignKey(
        "providers.ProviderModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_rating_display()} on message {self.message_id}"

    @property
    def model_label(self):
        """Whichever of the two model-snapshot fields is actually set -
        provider_model_used for every feedback entry since the
        ModelConfig -> ProviderModel cutover, model_used for everything
        before it. Templates should read this instead of model_used
        directly, or the Model column silently goes blank for every new
        entry (see governance/_feedback_table.html)."""
        if self.provider_model_used_id:
            return self.provider_model_used.display_label
        if self.model_used_id:
            return self.model_used.display_label
        return None


class PromptTemplate(models.Model):
    """A reusable prompt, either personal (owner set, visible only to that
    user) or department-wide (department set, admin-created, visible to
    everyone in that department alongside their own personal ones)."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="prompt_templates"
    )
    department = models.ForeignKey(
        "accounts.Department", on_delete=models.CASCADE, null=True, blank=True, related_name="prompt_templates"
    )
    name = models.CharField(max_length=100)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(owner__isnull=False, department__isnull=True)
                    | models.Q(owner__isnull=True, department__isnull=False)
                ),
                name="prompt_template_exactly_one_of_owner_or_department",
            ),
        ]

    @property
    def is_team_template(self):
        return self.department_id is not None

    def __str__(self):
        return self.name
