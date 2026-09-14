"""Data migration only - fixes an inconsistency between two of this
app's own earlier migrations: 0031 gave Demo and Basic a nonzero
monthly_research_limit (2 and 5, following the reference mockup's own
numeric column) without checking that 0029's own explicit tiering
("Demo off, Basic off, Advanced on, Full on" for the research
feature_flags flag) had already turned research OFF for both.

The result was a real, user-visible bug: governance.plans.
plan_capability_summary() only ever checks the NUMERIC limit for this
row, never the boolean flag - so it showed "Research" as included on
the Plans pages, and (via billing.invoicing._plan_line_item_description,
this session's own fix for the invoice description) on invoice line
items too - even though the actual chat composer never offers the
Research toggle for either plan (has_feature(user, "research") is
False for both), since that's a completely separate code path that
DOES check the flag. A Demo/Basic customer would have been shown
"Research" as something they're paying for that they can never actually
use.

Zeroes monthly_research_limit back to 0 for the two plans whose flag
says research is off, rather than flipping the flag on for them -
0029's tiering was a deliberate, explicit business decision made this
session, not an oversight; 0031's numeric column was the actual mistake.
Same UPDATE-only, keyed-by-exact-name, harmless-no-op-elsewhere
reasoning as every other data migration this session.
"""

from django.db import migrations

_PLANS_WITHOUT_RESEARCH = ["Demo", "Basic"]


def zero_out_research_limit(apps, schema_editor):
    Plan = apps.get_model("governance", "Plan")
    for name in _PLANS_WITHOUT_RESEARCH:
        plan = Plan.objects.filter(name=name).first()
        if plan is None:
            continue
        if not plan.feature_flags.get("research"):
            plan.monthly_research_limit = 0
            plan.save(update_fields=["monthly_research_limit"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("governance", "0031_configure_research_limits_for_known_plans"),
    ]

    operations = [
        migrations.RunPython(zero_out_research_limit, noop_reverse),
    ]
