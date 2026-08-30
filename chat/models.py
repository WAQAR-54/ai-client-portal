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
    model_config = models.ForeignKey(ModelConfig, on_delete=models.CASCADE, related_name="user_permissions")
    is_allowed = models.BooleanField(default=True)

    class Meta:
        unique_together = [["user", "model_config"]]

    def __str__(self):
        return f"{self.user} -> {self.model_config} ({'allowed' if self.is_allowed else 'denied'})"


class ActiveConversationManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class Conversation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations")
    title = models.CharField(max_length=200, default="New chat")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_pinned = models.BooleanField(default=False)
    pinned_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveConversationManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


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
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    attachment = models.FileField(upload_to="chat_attachments/%Y/%m/", null=True, blank=True)
    attachment_original_name = models.CharField(max_length=255, blank=True)
    attachment_size = models.PositiveIntegerField(null=True, blank=True, help_text="Bytes.")
    served_from_cache = models.BooleanField(
        default=False,
        help_text="This reply was served from the Redis response cache instead of "
        "calling the provider again - see chat/response_cache.py. tokens/estimated_cost "
        "still reflect what the (skipped) call would have cost, for usage-limit accounting; "
        "this flag is what the admin cost-saved metric sums over.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_rating_display()} on message {self.message_id}"


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
