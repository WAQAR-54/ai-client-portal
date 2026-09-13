"""Data migration only - fills in Plan.monthly_research_limit (added by
0030, right after this app's Demo/Basic/Advanced/Full tiering migration
0029) for those same exact plan names, closing the gap 0029's own
docstring disclosed: the reference mockup showed a numeric monthly
Research cap, but that field didn't exist yet when 0029 ran. Deliberately
a separate migration rather than editing 0029 in place - 0029 may already
be applied on a deployment by the time this ships, and migrations that
already ran should never be rewritten.

Same UPDATE-only, keyed-by-exact-name, harmless-no-op-elsewhere
reasoning as 0029: never a CREATE.

Tiering (matches the mockup's own suggested Research column):
    Demo        2   (the whole 14-day trial, not literally "per calendar
                     month" - the trial is shorter than a month anyway)
    Basic       5
    Advanced    25
    Full        100
"""

from django.db import migrations

_RESEARCH_LIMITS = {
    "Demo": 2,
    "Basic": 5,
    "Advanced": 25,
    "Full": 100,
}


def configure_research_limits(apps, schema_editor):
    Plan = apps.get_model("governance", "Plan")
    for name, limit in _RESEARCH_LIMITS.items():
        Plan.objects.filter(name=name).update(monthly_research_limit=limit)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("governance", "0030_plan_monthly_research_limit"),
    ]

    operations = [
        migrations.RunPython(configure_research_limits, noop_reverse),
    ]
