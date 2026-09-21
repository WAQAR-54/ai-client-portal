from django.db import migrations

TASK_NAME = "Maintenance windows: apply due start/end"


def seed_schedule(apps, schema_editor):
    """Registers the every-minute maintenance check in django-celery-beat's own (admin-editable) schedule tables, like
    the project's other periodic tasks. The site does not depend on it - the request middleware applies a window's due
    start/end by the clock - it only makes the audit row and the emails appear on time when nobody is visiting."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = IntervalSchedule.objects.get_or_create(every=1, period="minutes")
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={"interval": schedule, "task": "governance.tasks.advance_maintenance", "enabled": True},
    )


def unseed_schedule(apps, schema_editor):
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("governance", "0039_maintenance_window"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_schedule, unseed_schedule),
    ]
