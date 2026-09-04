from django.db import migrations


def remove_schedule(apps, schema_editor):
    """chat.tasks.check_for_new_models (and the legacy 2-provider "Sync
    Models" admin flow it supported - see chat/model_sync.py, now deleted)
    no longer exists: superseded by providers.tasks.sync_all_connected_
    providers, which does the same "notify SuperAdmins of new models"
    job for all 5 providers (see providers/migrations/
    0004_seed_provider_resync_schedule.py). Leaving this PeriodicTask row
    in place would make Celery beat try to run a task path that no
    longer resolves."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily new-model discovery check").delete()


def reverse(apps, schema_editor):
    """Re-seeds the same row 0005 originally created, in case this
    migration is ever rolled back - matches that migration's own
    schedule (4:30 daily) exactly."""
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
        name="Daily new-model discovery check",
        defaults={
            "crontab": schedule,
            "task": "chat.tasks.check_for_new_models",
            "enabled": True,
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0010_messagefeedback_provider_model_used"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(remove_schedule, reverse),
    ]
