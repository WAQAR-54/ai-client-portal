"""Data migration only - fixes RegionalPrice rows for plans named exactly
"Basic"/"Advanced"/"Full" that were left with leftover placeholder values
from early testing of the Regional Pricing feature: every region priced
Advanced BELOW Basic, and Full the SAME as Basic - no tier ordering at
all, which is exactly what made the public pricing page look broken
("bilkul fazool" - reported directly).

These are STILL placeholder numbers, not real business pricing - the
user explicitly asked to fix the ordering now (Basic < Advanced < Full,
same relative shape in every region) and provide real numbers later,
rather than have this migration guess at real prices. Whoever reviews
pricing next should treat every value here as provisional and update it
from the Regional Pricing admin page - this migration exists purely so
the page stops contradicting itself in the meantime.

Deliberately a plain UPDATE keyed by exact plan name, never a CREATE -
same reasoning as governance's 0029/0031: harmless no-op on any
deployment without plans of these exact names, and never touches Demo
(excluded from the public pricing page already, and not part of what
was reported broken here).
"""

from django.db import migrations

_PLACEHOLDER_PRICES = {
    # plan name -> {region_code: price}
    "Basic": {"PK": 4000, "SA": 109, "AE": 109, "ROW": 29},
    "Advanced": {"PK": 8000, "SA": 219, "AE": 219, "ROW": 59},
    "Full": {"PK": 15000, "SA": 369, "AE": 369, "ROW": 99},
}


def fix_pricing_order(apps, schema_editor):
    Plan = apps.get_model("governance", "Plan")
    RegionalPrice = apps.get_model("billing", "RegionalPrice")

    for plan_name, region_prices in _PLACEHOLDER_PRICES.items():
        plan = Plan.objects.filter(name=plan_name).first()
        if plan is None:
            continue
        for region_code, price in region_prices.items():
            RegionalPrice.objects.update_or_create(plan=plan, region_code=region_code, defaults={"price": price})


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0012_seed_daily_overdue_reminder_schedule"),
    ]

    operations = [
        migrations.RunPython(fix_pricing_order, noop_reverse),
    ]
