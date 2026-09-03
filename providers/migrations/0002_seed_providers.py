from django.db import migrations

# Seeded "not connected" so all 5 cards show up out of the box, matching
# the approved Providers page mockup - an admin pastes a key into one to
# actually connect it. adapter_type/base_url determine which adapter class
# handles each (see providers/adapters/__init__.py); OpenAI/Grok/DeepSeek
# share the same OpenAI-compatible adapter, only base_url differs (and
# Grok/DeepSeek's are even built into that adapter as defaults - see its
# own _BUILTIN_BASE_URLS - so base_url is left blank here for those two
# too, not duplicated).
SEED_PROVIDERS = [
    {"name": "Anthropic", "slug": "anthropic", "adapter_type": "anthropic", "base_url": ""},
    {"name": "OpenAI", "slug": "openai", "adapter_type": "openai_compatible", "base_url": "https://api.openai.com/v1"},
    {"name": "Google Gemini", "slug": "gemini", "adapter_type": "gemini", "base_url": ""},
    {"name": "xAI Grok", "slug": "grok", "adapter_type": "openai_compatible", "base_url": ""},
    {"name": "DeepSeek", "slug": "deepseek", "adapter_type": "openai_compatible", "base_url": ""},
]


def seed_providers(apps, schema_editor):
    Provider = apps.get_model("providers", "Provider")
    for entry in SEED_PROVIDERS:
        Provider.objects.get_or_create(slug=entry["slug"], defaults=entry)


def noop_reverse(apps, schema_editor):
    """Deliberately a no-op, same reasoning as governance's model/plan seed
    migrations - a Provider row may have real ProviderModel/key data
    attached by the time anyone reverses this, and deleting it would be
    more destructive than leaving it in place."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("providers", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_providers, noop_reverse),
    ]
