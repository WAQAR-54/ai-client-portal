from django.db import migrations

# Off by default for every normal role - Code Playground is reached only
# via its own direct URL (never linked from the sidebar nav), and a
# SuperAdmin opts a role in from the existing Feature Visibility page. A
# RoleFeatureToggle row's absence normally means "visible" (see
# governance/models.py's RoleFeatureToggle docstring) - this migration is
# what makes this one feature the deliberate exception.
ROLES = ["user", "manager", "admin"]


def seed_disabled(apps, schema_editor):
    RoleFeatureToggle = apps.get_model("governance", "RoleFeatureToggle")
    for role in ROLES:
        RoleFeatureToggle.objects.get_or_create(
            role=role, feature_key="code_playground", defaults={"is_enabled": False}
        )


def reverse(apps, schema_editor):
    RoleFeatureToggle = apps.get_model("governance", "RoleFeatureToggle")
    RoleFeatureToggle.objects.filter(role__in=ROLES, feature_key="code_playground").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0016_seed_pii_rules"),
    ]

    operations = [
        migrations.RunPython(seed_disabled, reverse),
    ]
