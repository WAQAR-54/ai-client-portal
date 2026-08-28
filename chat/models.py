from decimal import Decimal

from django.conf import settings
from django.db import models


class ModelConfig(models.Model):
    class Provider(models.TextChoices):
        OPENAI = "openai", "OpenAI"
        ANTHROPIC = "anthropic", "Anthropic"

    class Tier(models.TextChoices):
        ECONOMY = "economy", "Economy"
        DEFAULT = "default", "Default"
        PREMIUM = "premium", "Premium"

    provider = models.CharField(max_length=20, choices=Provider.choices)
    model_name = models.CharField(
        max_length=100,
        help_text="Exact provider API model identifier, e.g. gpt-4o-mini or claude-sonnet-4-5.",
    )
    tier = models.CharField(max_length=20, choices=Tier.choices, default=Tier.DEFAULT)
    input_cost_per_1m = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True,
        help_text="USD per 1M input tokens. Verify against provider pricing before setting.",
    )
    output_cost_per_1m = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True,
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

    def estimate_cost(self, input_tokens, output_tokens):
        if self.input_cost_per_1m is None or self.output_cost_per_1m is None:
            return None
        return (
            Decimal(input_tokens) * self.input_cost_per_1m
            + Decimal(output_tokens) * self.output_cost_per_1m
        ) / Decimal(1_000_000)


class UserModelPermission(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="model_permissions")
    model_config = models.ForeignKey(ModelConfig, on_delete=models.CASCADE, related_name="user_permissions")
    is_allowed = models.BooleanField(default=True)

    class Meta:
        unique_together = [["user", "model_config"]]

    def __str__(self):
        return f"{self.user} -> {self.model_config} ({'allowed' if self.is_allowed else 'denied'})"


class Conversation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations")
    title = models.CharField(max_length=200, default="New chat")
    created_at = models.DateTimeField(auto_now_add=True)

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
        ModelConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages",
    )
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    attachment = models.FileField(upload_to="chat_attachments/%Y/%m/", null=True, blank=True)
    attachment_original_name = models.CharField(max_length=255, blank=True)
    attachment_size = models.PositiveIntegerField(null=True, blank=True, help_text="Bytes.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"
