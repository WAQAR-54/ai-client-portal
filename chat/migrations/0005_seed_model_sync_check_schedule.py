from django.db import migrations


def seed_schedule(apps, schema_editor):
    """Daily check for new provider models, admin-editable like every other
    periodic task in this project (see notifications' own seed migration)."""
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


def reverse(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily new-model discovery check").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0004_modelconfig_display_name"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_schedule, reverse),
    ]
