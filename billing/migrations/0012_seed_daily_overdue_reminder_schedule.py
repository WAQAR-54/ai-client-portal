from django.db import migrations


def seed_schedule(apps, schema_editor):
    """Registers the daily overdue-reminder sweep in django-celery-beat's
    own (admin-editable) schedule tables - same pattern as billing/
    migrations/0009_seed_daily_invoice_sweep_schedule.py. 05:00, half an
    hour after that invoice-generation sweep's own 04:30 slot, so the
    reminder sweep always sees that run's freshly-generated invoices
    without racing it."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="5",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    PeriodicTask.objects.get_or_create(
        name="Daily overdue invoice reminder sweep",
        defaults={
            "crontab": schedule,
            "task": "billing.tasks.send_overdue_reminders",
            "enabled": True,
        },
    )


def reverse(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily overdue invoice reminder sweep").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0011_invoice_reminder_sent_at"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_schedule, reverse),
    ]
