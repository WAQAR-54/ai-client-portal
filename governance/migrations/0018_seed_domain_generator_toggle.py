from django.db import migrations

# Same off-by-default treatment as 0017_seed_code_playground_toggle -
# Domain Generator is a standalone tool reached only by direct link, and a
# SuperAdmin opts a role in from the Feature Visibility page.
ROLES = ["user", "manager", "admin"]


def seed_disabled(apps, schema_editor):
    RoleFeatureToggle = apps.get_model("governance", "RoleFeatureToggle")
    for role in ROLES:
        RoleFeatureToggle.objects.get_or_create(
            role=role, feature_key="domain_generator", defaults={"is_enabled": False}
        )


def reverse(apps, schema_editor):
    RoleFeatureToggle = apps.get_model("governance", "RoleFeatureToggle")
    RoleFeatureToggle.objects.filter(role__in=ROLES, feature_key="domain_generator").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0017_seed_code_playground_toggle"),
    ]

    operations = [
        migrations.RunPython(seed_disabled, reverse),
    ]
