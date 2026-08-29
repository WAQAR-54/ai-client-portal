from django.db import migrations


def grandfather_existing_users(apps, schema_editor):
    """0005 backfilled every pre-existing user onto the Demo plan using the
    same "default plan" as brand-new signups - which would have silently
    capped an already-active real customer account at 5,000 tokens/day,
    3 conversations/day, no file upload, with a 7-day countdown to full
    lockout. That's a real behavior regression with zero warning, not
    something a background migration should ever do. Re-point every
    assignment created by that backfill (assigned_by is null and it's
    still sitting on Demo) onto Premium instead - explicit and visible in
    the admin UI, and not more restrictive than "no limit at all", which
    is what these accounts actually had before Plans existed."""
    UserPlanAssignment = apps.get_model("governance", "UserPlanAssignment")
    Plan = apps.get_model("governance", "Plan")

    premium = Plan.objects.filter(name="Premium").first()
    demo = Plan.objects.filter(name="Demo").first()
    if not premium or not demo:
        return

    UserPlanAssignment.objects.filter(
        plan=demo,
        assigned_by__isnull=True,
        expires_at__isnull=True,
    ).update(plan=premium, previous_plan=demo)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("governance", "0005_seed_plans"),
    ]

    operations = [
        migrations.RunPython(grandfather_existing_users, reverse_noop),
    ]
