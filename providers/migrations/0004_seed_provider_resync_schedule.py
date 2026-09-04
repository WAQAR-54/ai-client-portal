from django.db import migrations


def seed_schedule(apps, schema_editor):
    """Daily resync of every connected Provider's model list, admin-editable
    like every other periodic task in this project (see notifications' and
    chat's own seed migrations) - a distinct time slot (5:00) from the
    legacy chat.tasks.check_for_new_models sweep (4:30) so the two never
    race each other."""
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
        name="Daily connected-provider model resync",
        defaults={
            "crontab": schedule,
            "task": "providers.tasks.sync_all_connected_providers",
            "enabled": True,
        },
    )


def reverse(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily connected-provider model resync").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("providers", "0003_providermodel_tier"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_schedule, reverse),
    ]
