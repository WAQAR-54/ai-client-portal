from decimal import Decimal

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


def _fernet():
    key = settings.FIELD_ENCRYPTION_KEY
    return Fernet(key.encode() if isinstance(key, str) else key)


class Provider(models.Model):
    """A connected (or connectable) AI provider - the org-wide credential
    and model registry that replaces the old settings.OPENAI_API_KEY /
    ANTHROPIC_API_KEY env vars and chat.models.ModelConfig. api_key_encrypted
    is never exposed outside get_decrypted_key(), which only the adapter
    layer calls to make an actual API request - every view/template/
    serializer must only ever see api_key_last4."""

    class AdapterType(models.TextChoices):
        ANTHROPIC = "anthropic", _("Anthropic")
        OPENAI_COMPATIBLE = "openai_compatible", _("OpenAI-compatible")
        GEMINI = "gemini", _("Google Gemini")

    class SyncStatus(models.TextChoices):
        NEVER = "never", _("Never synced")
        SUCCESS = "success", _("Success")
        FAILED = "failed", _("Failed")

    name = models.CharField(max_length=50)
    slug = models.SlugField(unique=True)
    adapter_type = models.CharField(max_length=50, choices=AdapterType.choices)
    # Only meaningful for OPENAI_COMPATIBLE providers that aren't one of the
    # built-in ones (Grok, DeepSeek) - those default their base_url in the
    # adapter itself (see providers/adapters/openai_compatible.py) so a
    # custom OpenAI-compatible provider (Mistral, Groq, a self-hosted
    # vLLM/Ollama endpoint, ...) is just a new row with this field set,
    # never a new adapter class.
    base_url = models.URLField(blank=True)
    api_key_encrypted = models.BinaryField(blank=True, default=b"")
    api_key_last4 = models.CharField(max_length=4, blank=True)
    is_connected = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_status = models.CharField(max_length=20, choices=SyncStatus.choices, default=SyncStatus.NEVER)
    last_sync_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def set_api_key(self, raw_key):
        """Encrypts and stores `raw_key`, and separately stores just its
        last 4 characters for masked display (`sk-...a91f`) - the ONLY
        piece of the key any view/template is allowed to read back."""
        self.api_key_encrypted = _fernet().encrypt(raw_key.encode())
        self.api_key_last4 = raw_key[-4:] if len(raw_key) >= 4 else raw_key

    def get_decrypted_key(self):
        """Internal use only (the adapter layer, to make an actual API
        call) - never call this from a view, template, or serializer."""
        if not self.api_key_encrypted:
            return None
        try:
            return _fernet().decrypt(bytes(self.api_key_encrypted)).decode()
        except InvalidToken:
            # FIELD_ENCRYPTION_KEY changed since this row was written (key
            # rotation without a re-encrypt pass) - treat as "no usable
            # key" rather than raising and taking down whatever page/task
            # touched this Provider.
            return None

    def get_adapter(self):
        from providers.adapters import get_adapter_class

        return get_adapter_class(self.adapter_type)(self)


class ProviderModel(models.Model):
    """One model as reported by a Provider's own API - the replacement for
    chat.models.ModelConfig. is_enabled is OFF by default for every newly
    discovered model (see providers/services.py:sync_provider) - a
    deliberate cost-control guardrail, never auto-widened by a sync."""

    class Tier(models.TextChoices):
        ECONOMY = "economy", _("Economy")
        DEFAULT = "default", _("Default")
        PREMIUM = "premium", _("Premium")

    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="models")
    model_id = models.CharField(max_length=100, help_text="Raw model identifier from the provider's own API.")
    display_name = models.CharField(max_length=100, blank=True)
    # No provider API reports "tier" - this was always an admin judgment
    # call on the old ModelConfig too, driving chat/router.py's smart-
    # routing cheapest-first-within-tier selection. Defaults to DEFAULT for
    # a freshly-synced model, same as ModelConfig.tier's own default -
    # left for an admin to actually classify (Economy/Premium) alongside
    # reviewing/enabling it.
    tier = models.CharField(max_length=20, choices=Tier.choices, default=Tier.DEFAULT)
    is_enabled = models.BooleanField(default=False)
    # True until an admin has reviewed (i.e. explicitly toggled, in either
    # direction) this model at least once - lets the Providers UI badge
    # "3 new models pending review" after a background resync finds
    # something, without that meaning it's usable yet.
    is_new = models.BooleanField(default=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    # True once a sync no longer sees this model in the provider's own
    # list - kept (never deleted) since Plan/Message history may still
    # reference it; is_enabled is forced off alongside this (see
    # sync_provider) so a retired model can't keep being assigned.
    is_retired = models.BooleanField(default=False)
    input_price_per_mtok = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    output_price_per_mtok = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)

    class Meta:
        ordering = ["provider__name", "model_id"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "model_id"], name="unique_model_id_per_provider"),
        ]

    def __str__(self):
        return f"{self.provider.name} / {self.model_id}"

    @property
    def display_label(self):
        return self.display_name or self.model_id

    def estimate_cost(self, input_tokens, output_tokens):
        """Same shape/behavior as the old chat.models.ModelConfig.
        estimate_cost - chat/views.py's cache-hit and per-message cost
        accounting call this on whatever select_model_candidates()
        returned, which is a ProviderModel now (see chat/router.py)."""
        if self.input_price_per_mtok is None or self.output_price_per_mtok is None:
            return None
        return (
            Decimal(input_tokens) * self.input_price_per_mtok + Decimal(output_tokens) * self.output_price_per_mtok
        ) / Decimal(1_000_000)
