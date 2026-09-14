"""Data migration only - the new show_on_public_pricing field defaults to
True on the schema, which would be WRONG for an existing demo/retired
plan (billing.views.PublicPricingView used to filter is_active=True,
is_demo=False - a demo or inactive plan never appeared there). Backfills
every existing row to match that exact prior visible set, so switching
PublicPricingView over to this new field is a zero-behavior-change
deploy until a SuperAdmin starts using the new toggle deliberately."""

from django.db import migrations


def backfill_show_on_public_pricing(apps, schema_editor):
    Plan = apps.get_model("governance", "Plan")
    Plan.objects.filter(is_active=True, is_demo=False).update(show_on_public_pricing=True)
    Plan.objects.exclude(is_active=True, is_demo=False).update(show_on_public_pricing=False)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("governance", "0033_plan_management_fields"),
    ]

    operations = [
        migrations.RunPython(backfill_show_on_public_pricing, noop_reverse),
    ]
