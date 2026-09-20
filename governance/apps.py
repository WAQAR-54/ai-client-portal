from django.apps import AppConfig


class GovernanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "governance"

    def ready(self):
        # Record each Celery task's outcome (governance/task_monitor.py). Connected in every process
        # that loads Django - web, worker and beat - and harmless where no task ever runs.
        from governance import task_monitor

        task_monitor.connect()
