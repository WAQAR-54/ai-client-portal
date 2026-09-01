from django.db import migrations


def backfill_model_selection_true(apps, schema_editor):
    """New KNOWN_FEATURE_FLAGS entries default to unset ("off") for a plan
    that predates them - correct for a brand-new flag with no prior behavior,
    but "model_selection" gates something that has ALWAYS worked (the chat
    model-select dropdown) up to this point. Without this backfill, every
    existing plan would silently lose that capability the moment this flag's
    enforcement ships. Explicitly setting it True here preserves current
    behavior for every plan that exists today; only plans created AFTER this
    migration default to off, matching every other flag in the list.
    """
    Plan = apps.get_model("governance", "Plan")
    for plan in Plan.objects.all():
        flags = plan.feature_flags or {}
        if "model_selection" not in flags:
            flags["model_selection"] = True
            plan.feature_flags = flags
            plan.save(update_fields=["feature_flags"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("governance", "0009_rolefeaturetoggle"),
    ]

    operations = [
        migrations.RunPython(backfill_model_selection_true, noop_reverse),
    ]
