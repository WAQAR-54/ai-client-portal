from django.db import migrations


def seed_pii_rules(apps, schema_editor):
    PIIRule = apps.get_model("governance", "PIIRule")
    defaults = [
        ("national_id", "redact"),
        ("credit_card", "block"),
        ("phone_number", "warn"),
    ]
    for kind, action in defaults:
        PIIRule.objects.get_or_create(kind=kind, defaults={"action": action, "is_enabled": False})


def reverse(apps, schema_editor):
    PIIRule = apps.get_model("governance", "PIIRule")
    PIIRule.objects.filter(kind__in=["national_id", "credit_card", "phone_number"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0015_compliancesettings_piirule"),
    ]

    operations = [
        migrations.RunPython(seed_pii_rules, reverse),
    ]
