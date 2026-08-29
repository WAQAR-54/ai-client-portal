from django.db import migrations


def seed_schedule(apps, schema_editor):
    """Registers the daily trial-expiry sweep in django-celery-beat's own
    (admin-editable) schedule tables, rather than hardcoding it in
    settings.py - matches this project's existing philosophy of keeping
    operational knobs admin-editable, not baked into code (see the Plan
    model). Only runs once a real Celery beat process is deployed with a
    configured broker; harmless to have registered before that."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="4",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    PeriodicTask.objects.get_or_create(
        name="Daily trial-expiry notification sweep",
        defaults={
            "crontab": schedule,
            "task": "notifications.tasks.sweep_expiring_demo_plans",
            "enabled": True,
        },
    )


def reverse(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily trial-expiry notification sweep").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_schedule, reverse),
    ]
