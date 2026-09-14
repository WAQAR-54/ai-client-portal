"""Data migration only - fixes the real root cause behind a directly
reported bug: a team invoice wasn't adding up per-seat at all ("plan ke
according plus nahi hui... 3 seats allow karega na" - an Advanced-plan
team of 3 should cost 3x, not the flat 1-seat price).

generate_invoice_for_department (billing/invoicing.py) only ever adds an
extra line item for seats beyond Plan.seats_included when that region's
RegionalPrice.extra_seat_price is set - and for every real Demo/Basic/
Advanced/Full plan, in every region, nobody had ever configured it via
the Regional Pricing admin page, so it was None everywhere. Since every
real plan's seats_included is 1, this meant a team/department of any
size was always billed the same flat 1-seat price no matter how many
people were actually in it - the extra-seat mechanism existed and works
(see billing/tests.py's own coverage using an explicit extra_seat_price
fixture), it just had no real data to act on.

Sets extra_seat_price equal to that SAME region's own base price for
every plan/region still unset - not a new, invented number: the base
price already represents "cost per seat" (seats_included=1 on every
real plan), so charging the same rate for each additional person turns
this into a straightforward price x headcount total, using exactly the
numbers already configured. Deliberately only touches rows that are
still None - never overwrites a value a SuperAdmin may have already
set intentionally.
"""

from django.db import migrations

_PLAN_NAMES = ["Demo", "Basic", "Advanced", "Full"]


def set_extra_seat_price(apps, schema_editor):
    RegionalPrice = apps.get_model("billing", "RegionalPrice")
    rows = RegionalPrice.objects.filter(plan__name__in=_PLAN_NAMES, extra_seat_price__isnull=True, price__isnull=False)
    for regional_price in rows:
        regional_price.extra_seat_price = regional_price.price
        regional_price.save(update_fields=["extra_seat_price"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0013_fix_placeholder_pricing_order_for_known_plans"),
    ]

    operations = [
        migrations.RunPython(set_extra_seat_price, noop_reverse),
    ]
