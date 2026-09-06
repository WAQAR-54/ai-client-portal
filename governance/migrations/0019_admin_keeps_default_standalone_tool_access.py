from django.db import migrations

# 0017/0018 seeded an explicit is_enabled=False row for "admin" too, which
# accidentally left the admin-role checkbox in Feature Visibility inert:
# governance.features.user_can_access_standalone_tool used to give
# Admin/SuperAdmin access unconditionally regardless of that row, so
# turning it off there did nothing - and the admin Dashboard's "Open X" /
# stats panel stayed visible to Admin even when a SuperAdmin had switched
# it off for their role. Removing the row restores the normal "no row =
# visible" default for admin (same as every other feature), while
# SuperAdmin explicitly toggling it off from Feature Visibility now
# actually takes effect for real Admins too.
FEATURE_KEYS = ["code_playground", "domain_generator"]


def remove_admin_rows(apps, schema_editor):
    RoleFeatureToggle = apps.get_model("governance", "RoleFeatureToggle")
    RoleFeatureToggle.objects.filter(role="admin", feature_key__in=FEATURE_KEYS).delete()


def reverse(apps, schema_editor):
    RoleFeatureToggle = apps.get_model("governance", "RoleFeatureToggle")
    for feature_key in FEATURE_KEYS:
        RoleFeatureToggle.objects.get_or_create(role="admin", feature_key=feature_key, defaults={"is_enabled": False})


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0018_seed_domain_generator_toggle"),
    ]

    operations = [
        migrations.RunPython(remove_admin_rows, reverse),
    ]
