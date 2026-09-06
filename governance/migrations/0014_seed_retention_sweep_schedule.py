from django.db import migrations


def seed_schedule(apps, schema_editor):
    """Registers the daily conversation-retention sweep in django-celery-
    beat's own schedule tables - same pattern as notifications/migrations/
    0002_seed_daily_expiry_sweep_schedule.py. Harmless before a real Celery
    beat process with a configured broker is deployed."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="4",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    PeriodicTask.objects.get_or_create(
        name="Daily conversation-retention sweep",
        defaults={
            "crontab": schedule,
            "task": "governance.tasks.sweep_conversation_retention",
            "enabled": True,
        },
    )


def reverse(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily conversation-retention sweep").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0013_routingrule"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_schedule, reverse),
    ]
