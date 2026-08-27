from django.core.management.base import BaseCommand

from chat.models import ModelConfig

# Model identifiers only — no pricing. Pricing must be verified against
# current provider docs and entered in Django admin before enabling a model
# (see AI_Client_Portal_Spec.md section 8).
SEED_MODELS = [
    (ModelConfig.Provider.OPENAI, "gpt-4o-mini", ModelConfig.Tier.ECONOMY),
    (ModelConfig.Provider.OPENAI, "gpt-4o", ModelConfig.Tier.DEFAULT),
    (ModelConfig.Provider.ANTHROPIC, "claude-haiku-4-5-20251001", ModelConfig.Tier.ECONOMY),
    (ModelConfig.Provider.ANTHROPIC, "claude-sonnet-5", ModelConfig.Tier.DEFAULT),
    (ModelConfig.Provider.ANTHROPIC, "claude-opus-5", ModelConfig.Tier.PREMIUM),
]


class Command(BaseCommand):
    help = "Seed ModelConfig with known model identifiers (pricing left blank, disabled by default)."

    def handle(self, *args, **options):
        created_count = 0
        for provider, model_name, tier in SEED_MODELS:
            _, created = ModelConfig.objects.get_or_create(
                provider=provider, model_name=model_name, defaults={"tier": tier},
            )
            created_count += int(created)

        self.stdout.write(self.style.SUCCESS(f"Seeded {created_count} new model(s)."))
        self.stdout.write(
            "All seeded models are disabled and have no pricing set. "
            "Verify current pricing against provider docs, then enable in /admin/."
        )
