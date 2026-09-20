"""Background tasks are time-limited, and the limits actually reach Celery."""

from django.conf import settings
from django.test import SimpleTestCase

from config.celery import app


class CeleryLimitsTests(SimpleTestCase):
    def test_a_hung_task_can_no_longer_hold_a_worker_forever(self):
        self.assertGreater(settings.CELERY_TASK_TIME_LIMIT, 0)
        self.assertGreater(settings.CELERY_TASK_SOFT_TIME_LIMIT, 0)
        self.assertLess(settings.CELERY_TASK_SOFT_TIME_LIMIT, settings.CELERY_TASK_TIME_LIMIT)

    def test_the_limits_are_what_the_celery_app_actually_uses(self):
        self.assertEqual(app.conf.task_time_limit, settings.CELERY_TASK_TIME_LIMIT)
        self.assertEqual(app.conf.task_soft_time_limit, settings.CELERY_TASK_SOFT_TIME_LIMIT)

    def test_retries_are_bounded_on_every_task_that_retries(self):
        """autoretry_for without max_retries would retry (with growing back-off) for ever."""
        app.loader.import_default_modules()
        unbounded = [
            name
            for name, task in app.tasks.items()
            if not name.startswith("celery.") and getattr(task, "autoretry_for", None) and task.max_retries is None
        ]
        self.assertEqual(unbounded, [])
