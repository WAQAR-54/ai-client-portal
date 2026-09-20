"""Background-task outcomes come from Celery's real signals, not from guesses.

Regression for: the System Status page could only say a task was "dispatched" - nothing recorded
whether it succeeded, failed, how long it took, or whether the scheduler had gone quiet."""

from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from celery.signals import task_retry
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from accounts.models import User
from config.celery import app
from governance import task_monitor as tm
from governance.system_status import check_jobs

BOOM = "governance.test_task_monitor.boom"
OK = "governance.test_task_monitor.fine"


@app.task(name=BOOM)
def boom():
    raise ValueError("password=hunter2 in the exception text")


@app.task(name=OK)
def fine():
    return "done"


def periodic(name, task, every=5, period=IntervalSchedule.MINUTES, last_run_ago=None, enabled=True):
    schedule, _ = IntervalSchedule.objects.get_or_create(every=every, period=period)
    return PeriodicTask.objects.create(
        name=name,
        task=task,
        interval=schedule,
        enabled=enabled,
        last_run_at=timezone.now() - last_run_ago if last_run_ago is not None else None,
    )


class RecordingTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_a_successful_run_records_success_duration_and_clears_running(self):
        fine.apply()
        summary = tm.summarize(OK)
        self.assertEqual((summary["outcome"], summary["runs"], summary["running"]), ("ok", 1, False))
        self.assertIsNotNone(summary["last_success_at"])
        self.assertIsNotNone(summary["last_duration_ms"])

    def test_a_failure_records_the_exception_class_and_never_its_message(self):
        boom.apply(throw=False)
        summary = tm.summarize(BOOM)
        self.assertEqual((summary["outcome"], summary["failures"]), ("failing", 1))
        self.assertEqual(summary["last_failure_kind"], "ValueError")
        raw = repr(tm.get_record(BOOM))
        for secret in ("hunter2", "password", "exception text"):
            self.assertNotIn(secret, raw)

    def test_a_later_success_clears_the_failing_state_but_keeps_the_history(self):
        cache.set(tm._key(OK), {"last_failure_at": (timezone.now() - timedelta(hours=1)).isoformat(), "failures": 2})
        fine.apply()
        summary = tm.summarize(OK)
        self.assertEqual((summary["outcome"], summary["failures"]), ("ok", 2))

    def test_retries_are_counted(self):
        task_retry.send(sender=fine, request=None, reason="x", einfo=None)
        self.assertEqual(tm.summarize(OK)["retries"], 1)

    def test_a_task_that_never_reported_is_unknown_not_ok(self):
        summary = tm.summarize("never.ran")
        self.assertEqual((summary["recorded"], summary["outcome"]), (False, "unknown"))

    def test_a_running_task_is_shown_and_an_abandoned_marker_is_not(self):
        tm.on_prerun(task_id="t1", task=SimpleNamespace(name=OK))
        self.assertTrue(tm.summarize(OK)["running"])
        old = (timezone.now() - timedelta(hours=3)).isoformat()
        cache.set(tm._key(OK), {"running_since": old, "running_id": "t1"})
        summary = tm.summarize(OK)
        self.assertEqual((summary["running"], summary["abandoned"]), (False, True))

    def test_a_cache_outage_never_breaks_a_task(self):
        boom_error = ConnectionError("redis down")
        with patch.object(tm.cache, "get", side_effect=boom_error), patch.object(
            tm.cache, "set", side_effect=boom_error
        ):
            self.assertEqual(fine.apply().get(), "done")
            self.assertFalse(tm.summarize(OK)["recorded"])

    def test_signals_are_connected_exactly_once(self):
        from celery.signals import task_postrun

        before = len(task_postrun.receivers)
        tm.connect()
        tm.connect()
        self.assertEqual(len(task_postrun.receivers), before)

    def test_recording_writes_nothing_to_the_database(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            fine.apply()
        self.assertEqual(len(queries), 0)


class StalenessTests(TestCase):
    def test_an_interval_task_is_stale_after_three_missed_intervals(self):
        self.assertFalse(tm.is_stale(periodic("fresh", OK, every=5, last_run_ago=timedelta(minutes=12))))
        self.assertTrue(tm.is_stale(periodic("late", OK, every=30, last_run_ago=timedelta(hours=3))))

    def test_short_intervals_get_a_ten_minute_floor(self):
        self.assertFalse(tm.is_stale(periodic("quick", OK, every=1, last_run_ago=timedelta(minutes=8))))

    def test_disabled_and_never_dispatched_tasks_are_not_stale(self):
        self.assertFalse(tm.is_stale(periodic("off", OK, last_run_ago=timedelta(days=9), enabled=False)))
        self.assertFalse(tm.is_stale(periodic("new", OK, last_run_ago=None)))


class SystemStatusJobsTests(TestCase):
    def setUp(self):
        cache.clear()

    def rows(self):
        return {row["name"]: row for row in check_jobs()["rows"]}

    def test_health_reflects_the_recorded_outcome(self):
        periodic("ok-job", OK, last_run_ago=timedelta(minutes=1))
        periodic("bad-job", BOOM, last_run_ago=timedelta(minutes=1))
        periodic("silent-job", "never.ran", last_run_ago=timedelta(minutes=1))
        fine.apply()
        boom.apply(throw=False)
        rows = self.rows()
        self.assertEqual(rows["ok-job"]["health"], "ok")
        self.assertEqual(rows["bad-job"]["health"], "failing")
        self.assertEqual(rows["bad-job"]["failure_kind"], "ValueError")
        self.assertEqual(rows["silent-job"]["health"], "unknown")
        jobs = check_jobs()
        self.assertEqual((jobs["failing"], jobs["outcomes_recorded"]), (1, 2))

    def test_a_silent_scheduler_is_reported_stale_only_when_every_interval_task_is_late(self):
        periodic("a", OK, every=5, last_run_ago=timedelta(hours=2))
        periodic("b", "other.task", every=10, last_run_ago=timedelta(hours=3))
        self.assertTrue(check_jobs()["beat_stale"])
        periodic("c", "third.task", every=5, last_run_ago=timedelta(minutes=2))
        self.assertFalse(check_jobs()["beat_stale"])

    def test_no_tasks_is_not_a_stale_scheduler(self):
        self.assertFalse(check_jobs()["beat_stale"])

    def test_the_dashboard_shows_the_outcome_to_a_superadmin(self):
        periodic("bad-job", BOOM, last_run_ago=timedelta(minutes=1))
        boom.apply(throw=False)
        admin = User.objects.create_user(email="sys@example.com", password="pw12345!", role=User.Role.SUPERADMIN)
        self.client.force_login(admin)
        response = self.client.get(reverse("governance:dashboard"))
        self.assertContains(response, "ValueError")
        self.assertNotContains(response, "hunter2")
        self.assertContains(response, "Failing")


class OpsVerifyOutcomeTests(TestCase):
    def setUp(self):
        cache.clear()

    def output(self):
        out = StringIO()
        call_command("ops_verify", "--skip-feeds", stdout=out)
        return out.getvalue()

    def test_a_failed_task_is_a_failure_line(self):
        periodic("bad-job", BOOM, last_run_ago=timedelta(minutes=1))
        boom.apply(throw=False)
        self.assertIn("FAIL beat: tasks whose last run failed: ['bad-job (ValueError)']", self.output())

    def test_late_tasks_are_named(self):
        periodic("late-job", OK, every=5, last_run_ago=timedelta(hours=4))
        self.assertIn("WARN beat: interval tasks dispatched later than 3 intervals ago: ['late-job']", self.output())
