from django.db import migrations


def seed_schedule(apps, schema_editor):
    """Registers the daily invoice sweep in django-celery-beat's own
    (admin-editable) schedule tables - same pattern as notifications/
    migrations/0002_seed_daily_expiry_sweep_schedule.py. 04:30, a half
    hour after that trial-expiry sweep's own 04:00 slot, so the two daily
    tasks don't contend."""
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
        name="Daily invoice generation sweep",
        defaults={
            "crontab": schedule,
            "task": "billing.tasks.sweep_due_invoices",
            "enabled": True,
        },
    )


def reverse(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily invoice generation sweep").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0008_alter_invoice_department"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_schedule, reverse),
    ]
