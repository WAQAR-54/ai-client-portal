from django.db import migrations


def seed_schedule(apps, schema_editor):
    """Registers the daily database backup in django-celery-beat's own
    (admin-editable) schedule tables - see accounts/tasks.py for why this
    replaces the previously-manual "set up a VPS crontab entry" step from
    docs/BACKUP_RESTORE.md. 03:00 UTC, ahead of every other scheduled job
    in this app (which run at 04:00/04:30/05:00) so a fresh backup exists
    before any of them run."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="3",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    PeriodicTask.objects.get_or_create(
        name="Daily database backup",
        defaults={
            "crontab": schedule,
            "task": "accounts.tasks.run_scheduled_database_backup",
            "enabled": True,
        },
    )


def reverse(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily database backup").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_user_google_email_user_google_linked_at_and_more"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_schedule, reverse),
    ]
