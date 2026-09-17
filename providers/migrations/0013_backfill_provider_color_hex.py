from django.db import migrations

# Matches static/css/main.css's existing --provider-* light-mode hex values
# exactly (line ~120-127) - so a provider that already had a hardcoded CSS
# color keeps looking exactly the same once accent_color() reads from the
# database instead. Any provider slug NOT in this dict (a newly-connected
# one, or a custom OpenAI-compatible provider) is deliberately left blank -
# Provider.accent_color() derives a deterministic color for it on the fly.
SEEDED_PROVIDER_COLORS = {
    "anthropic": "#122268",
    "openai": "#1f9254",
    "gemini": "#6d5bd0",
    "grok": "#b4790c",
    "deepseek": "#0e7c86",
}


def backfill(apps, schema_editor):
    Provider = apps.get_model("providers", "Provider")
    for slug, color_hex in SEEDED_PROVIDER_COLORS.items():
        Provider.objects.filter(slug=slug).update(color_hex=color_hex)


def reverse(apps, schema_editor):
    Provider = apps.get_model("providers", "Provider")
    Provider.objects.filter(slug__in=SEEDED_PROVIDER_COLORS.keys()).update(color_hex="")


class Migration(migrations.Migration):

    dependencies = [
        ("providers", "0012_provider_color_hex"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse),
    ]
