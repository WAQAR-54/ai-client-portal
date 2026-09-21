"""SuperAdmin "Server health" panel of System status (governance/server_health.py)."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from governance import server_health

STAT_FIRST = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 100 0 50 800 50 0 0 0 0 0\n"  # busy 150 / total 1000
STAT_SECOND = "cpu  120 0 60 860 60 0 0 0 0 0\n"  # busy 180 / total 1100: 30 of 100 jiffies busy = 30 %
MEMINFO = "MemTotal:        6000000 kB\nMemFree:          100000 kB\nMemAvailable:    3000000 kB\n"


def fake_proc(stat=STAT_FIRST, meminfo=MEMINFO, loadavg="0.82 0.70 0.61 1/200 999\n", uptime="1209600.55 4000.00\n"):
    """A directory shaped like /proc; a None file is left out (unreadable)."""
    root = Path(tempfile.mkdtemp())
    for name, text in (("stat", stat), ("meminfo", meminfo), ("loadavg", loadavg), ("uptime", uptime)):
        if text is not None:
            (root / name).write_text(text)
    return root


def make(email, role):
    return User.objects.create_user(email=email, password="pw12345!", role=role)


class ReadingsTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_real_values_come_from_the_proc_files(self):
        with patch.object(server_health, "PROC", fake_proc()):
            snapshot = server_health.collect_snapshot(now=1000.0)
        self.assertEqual(snapshot["memory"]["total"], 6000000 * 1024)
        self.assertAlmostEqual(snapshot["memory"]["percent"], 50.0)  # (6.0M - 3.0M available) / 6.0M
        self.assertEqual(snapshot["uptime"], 1209600.55)
        self.assertEqual(
            (snapshot["load"]["one"], snapshot["load"]["five"], snapshot["load"]["fifteen"]), (0.82, 0.70, 0.61)
        )
        self.assertIsNotNone(snapshot["disk"])  # statvfs on the app volume works on every OS

    def test_cpu_is_the_utilisation_between_two_collections(self):
        with patch.object(server_health, "PROC", fake_proc(STAT_FIRST)):
            server_health.read_cpu(now=1000.0)  # first reading: takes a short sample (nothing changes in a fake file)
        with patch.object(server_health, "PROC", fake_proc(STAT_SECOND)):
            cpu = server_health.read_cpu(now=1060.0)
        self.assertAlmostEqual(cpu["percent"], 30.0)
        self.assertEqual(cpu["window_seconds"], 60)

    def test_with_no_previous_reading_one_short_sample_is_taken(self):
        with patch.object(server_health, "_cpu_counters", side_effect=[(150, 1000), (180, 1100)]), patch.object(
            server_health.time, "sleep"
        ) as slept:
            cpu = server_health.read_cpu(now=5000.0)
        slept.assert_called_once_with(server_health.CPU_QUICK_SAMPLE_SECONDS)
        self.assertAlmostEqual(cpu["percent"], 30.0)
        self.assertEqual(cpu["window_seconds"], server_health.CPU_QUICK_SAMPLE_SECONDS)

    def test_a_stale_previous_reading_is_not_used_as_current_utilisation(self):
        cache.set(server_health.CPU_PREVIOUS_KEY, {"at": 1000.0, "counters": (0, 10)}, 3600)
        with patch.object(server_health, "_cpu_counters", side_effect=[(150, 1000), (180, 1100)]), patch.object(
            server_health.time, "sleep"
        ) as slept:
            cpu = server_health.read_cpu(now=1000.0 + server_health.CPU_PREVIOUS_MAX_AGE_SECONDS + 1)
        slept.assert_called_once()  # sampled afresh instead of averaging over hours
        self.assertAlmostEqual(cpu["percent"], 30.0)

    def test_missing_or_malformed_files_are_unavailable_never_zero(self):
        for kwargs, key in (
            ({"stat": None}, "cpu"),
            ({"stat": "garbage\n"}, "cpu"),
            ({"meminfo": None}, "memory"),
            ({"meminfo": "MemTotal: lots kB\nMemAvailable: 1 kB\n"}, "memory"),
            ({"meminfo": "MemTotal: 100 kB\nMemAvailable: 900 kB\n"}, "memory"),  # available > total is not believable
            ({"loadavg": None}, "load"),
            ({"loadavg": "x y z\n"}, "load"),
            ({"uptime": None}, "uptime"),
            ({"uptime": "soon\n"}, "uptime"),
        ):
            with self.subTest(kwargs=kwargs), patch.object(server_health, "PROC", fake_proc(**kwargs)):
                cache.clear()
                self.assertIsNone(server_health.collect_snapshot(now=1.0)[key])

    def test_no_proc_at_all_leaves_only_the_disk(self):
        with patch.object(server_health, "PROC", Path(tempfile.mkdtemp()) / "missing"):
            snapshot = server_health.collect_snapshot(now=1.0)
        self.assertEqual([snapshot[k] for k in ("cpu", "memory", "uptime", "load")], [None] * 4)
        self.assertIsNotNone(snapshot["disk"])

    def test_a_reader_that_raises_is_unavailable_and_does_not_stop_the_others(self):
        with patch.object(server_health, "PROC", fake_proc()), patch.object(
            server_health, "read_disk", side_effect=RuntimeError("x")
        ):
            snapshot = server_health.collect_snapshot(now=1.0)
        self.assertIsNone(snapshot["disk"])
        self.assertIsNotNone(snapshot["memory"])


class ThresholdTests(SimpleTestCase):
    def test_states_at_the_boundaries(self):
        self.assertEqual(server_health.state_for(69.9, 70, 85), "healthy")
        self.assertEqual(server_health.state_for(70, 70, 85), "warning")
        self.assertEqual(server_health.state_for(84.9, 70, 85), "warning")
        self.assertEqual(server_health.state_for(85, 70, 85), "critical")
        self.assertEqual(server_health.state_for(None, 70, 85), "unavailable")

    def snapshot(self, cpu, memory, disk):
        def pct(value):
            if value is None:
                return None
            return {"percent": value, "total": 10 * 1024**3, "used": value / 10 * 1024**3}

        return {
            "collected_at": 0,
            "cpu": None if cpu is None else {"percent": cpu, "window_seconds": 60},
            "memory": pct(memory),
            "disk": pct(disk),
            "uptime": 90000.0,
            "load": {"one": 0.5, "five": 0.4, "fifteen": 0.3, "cores": 1},
        }

    def states(self, **kwargs):
        return {m["key"]: m["state"] for m in server_health.build_metrics(self.snapshot(**kwargs))}

    def test_default_thresholds_for_cpu_memory_and_disk(self):
        self.assertEqual(self.states(cpu=58, memory=67, disk=34)["cpu"], "healthy")
        states = self.states(cpu=75, memory=80, disk=85)
        self.assertEqual((states["cpu"], states["memory"], states["disk"]), ("warning", "warning", "warning"))
        states = self.states(cpu=90, memory=95, disk=95)
        self.assertEqual((states["cpu"], states["memory"], states["disk"]), ("critical", "critical", "critical"))

    @override_settings(
        MEDIA_DISK_WARN_PCT=50,
        MEDIA_DISK_CRITICAL_PCT=60,
        SERVER_HEALTH_CPU_WARN_PCT=10,
        SERVER_HEALTH_CPU_CRITICAL_PCT=20,
    )
    def test_configured_thresholds_are_used_and_disk_reuses_the_media_ones(self):
        states = self.states(cpu=15, memory=10, disk=55)
        self.assertEqual((states["cpu"], states["disk"], states["memory"]), ("warning", "warning", "healthy"))

    def test_an_unavailable_reading_has_no_value_and_no_percentage(self):
        metrics = {m["key"]: m for m in server_health.build_metrics(self.snapshot(cpu=None, memory=None, disk=None))}
        for key in ("cpu", "memory", "disk"):
            self.assertFalse(metrics[key]["available"])
            self.assertEqual((metrics[key]["value"], metrics[key]["state"]), ("", "unavailable"))

    def test_formatting(self):
        self.assertEqual(server_health.format_uptime(14 * 86400 + 3600 * 2), "14 days 2 h")
        self.assertEqual(server_health.format_uptime(86400), "1 day")
        self.assertEqual(server_health.format_uptime(3 * 3600 + 300), "3 h 5 min")
        self.assertEqual(server_health.format_bytes(2.1 * 1024**3), "2.1 GB")
        self.assertEqual(server_health.ago(42), "42 seconds")
        self.assertEqual(server_health.ago(1), "1 second")
        self.assertEqual(server_health.ago(150), "2 minutes")


class ServicesTests(SimpleTestCase):
    def jobs(self, rows=(), beat_stale=False, last_dispatch_age=""):
        return {"rows": list(rows), "beat_stale": beat_stale, "last_dispatch_age": last_dispatch_age}

    def row(self, health):
        return {"enabled": True, "judged": True, "health": health}

    def states(self, database="healthy", redis="healthy", **jobs):
        services = server_health.build_services({"state": database}, {"state": redis}, self.jobs(**jobs))
        return {s["key"]: s["state"] for s in services}

    def test_only_what_the_existing_checks_support_is_claimed(self):
        self.assertEqual(
            self.states(rows=[self.row("ok")], last_dispatch_age="3 minutes"),
            {"web": "healthy", "postgres": "healthy", "redis": "healthy", "worker": "healthy", "beat": "healthy"},
        )
        idle = self.states()
        self.assertEqual((idle["worker"], idle["beat"]), ("unavailable", "unavailable"))  # nothing recorded: no claim

    def test_failures_are_reported_at_the_right_severity(self):
        states = self.states(database="unavailable", redis="unavailable", rows=[self.row("failing")], beat_stale=True)
        self.assertEqual(
            (states["postgres"], states["redis"], states["worker"], states["beat"]),
            ("critical", "critical", "warning", "critical"),
        )

    def test_an_unconfigured_redis_is_unavailable_not_broken(self):
        self.assertEqual(self.states(redis="not_configured")["redis"], "unavailable")


class CacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_readings_are_reused_within_the_refresh_window(self):
        with patch.object(
            server_health, "collect_snapshot", return_value={"collected_at": 1000.0}
        ) as collect, patch.object(server_health.time, "time", return_value=1010.0):
            for _ in range(5):
                server_health.get_snapshot()
        collect.assert_called_once()

    def test_readings_are_taken_again_once_the_window_has_passed(self):
        clock = {"now": 1000.0}
        with patch.object(
            server_health, "collect_snapshot", side_effect=lambda: {"collected_at": clock["now"]}
        ) as collect, patch.object(server_health.time, "time", side_effect=lambda: clock["now"]):
            server_health.get_snapshot()
            clock["now"] = 1000.0 + settings.SERVER_HEALTH_CACHE_SECONDS + 1
            server_health.get_snapshot()
        self.assertEqual(collect.call_count, 2)

    def test_a_broken_cache_does_not_break_the_panel(self):
        with patch.object(server_health, "PROC", fake_proc()), patch.object(
            cache, "get", side_effect=OSError("down")
        ), patch.object(cache, "set", side_effect=OSError("down")), patch.object(server_health.time, "sleep"):
            snapshot = server_health.get_snapshot()
        self.assertIsNotNone(snapshot["memory"])


class PanelViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.superadmin = make("root@example.com", User.Role.SUPERADMIN)
        self.admin = make("admin@example.com", User.Role.ADMIN)
        self.user = make("user@example.com", User.Role.USER)

    def login(self, user):
        self.client.force_login(user)

    def dashboard(self):
        return self.client.get(reverse("governance:dashboard"))

    def test_the_superadmin_sees_the_panel_on_system_status(self):
        self.login(self.superadmin)
        with patch.object(server_health, "PROC", fake_proc()):
            response = self.dashboard()
        self.assertContains(response, 'data-panel="server-health"')
        self.assertContains(response, "Server health")
        self.assertContains(response, 'data-metric="memory"')
        self.assertContains(response, "50%")  # memory, from the fake /proc/meminfo
        self.assertContains(response, "14 days")
        self.assertContains(response, "Last updated:")
        self.assertContains(response, "Docker status unavailable")
        for name in ("Web", "Worker", "Beat", "PostgreSQL", "Redis"):
            self.assertContains(response, name)

    def test_an_admin_does_not_get_the_panel_and_cannot_fetch_it(self):
        self.login(self.admin)
        self.assertNotContains(self.dashboard(), "server-health")
        self.assertEqual(self.client.get(reverse("governance:server_health")).status_code, 403)

    def test_a_normal_user_and_an_anonymous_visitor_are_refused(self):
        self.login(self.user)
        self.assertEqual(self.client.get(reverse("governance:server_health")).status_code, 403)
        self.client.logout()
        response = self.client.get(reverse("governance:server_health"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_the_refresh_endpoint_returns_only_the_panel_and_polls_at_a_calm_rate(self):
        self.login(self.superadmin)
        with patch.object(server_health, "PROC", fake_proc()):
            response = self.client.get(reverse("governance:server_health"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="server-health"', html)
        self.assertNotIn("sys-status-title", html)  # not the whole System status section
        self.assertIn('hx-trigger="every 60s [!document.hidden]"', html)  # not every second, and not in a hidden tab
        self.assertIn('hx-swap="outerHTML"', html)

    def test_refreshing_does_not_collect_again_inside_the_window(self):
        self.login(self.superadmin)
        with patch.object(server_health, "PROC", fake_proc()), patch.object(
            server_health, "collect_snapshot", wraps=server_health.collect_snapshot
        ) as collect:
            self.dashboard()
            self.client.get(reverse("governance:server_health"))
            self.client.get(reverse("governance:server_health"))
        collect.assert_called_once()

    def test_an_unreadable_metric_shows_unavailable_and_the_dashboard_still_renders(self):
        self.login(self.superadmin)
        with patch.object(server_health, "PROC", Path(tempfile.mkdtemp()) / "missing"):
            response = self.dashboard()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        panel = html[html.index('data-panel="server-health"') :]
        panel = panel[: panel.index("</article>")]
        for key in ("cpu", "memory", "uptime", "load"):
            tile = panel[panel.index(f'data-metric="{key}"') :]
            tile = tile[: tile.index("</div>\n        </div>") if "</div>\n        </div>" in tile else len(tile)]
            self.assertIn("Unavailable", tile, key)
        self.assertNotIn("0%", panel)  # never a fake 0 %
        self.assertContains(response, 'data-metric="disk"')  # the reading that does exist is still shown
        self.assertContains(response, "System status")  # the rest of the section is intact

    def test_no_secret_path_or_infrastructure_detail_is_rendered(self):
        self.login(self.superadmin)
        with patch.object(server_health, "PROC", fake_proc()):
            html = self.client.get(reverse("governance:server_health")).content.decode()
        for forbidden in (
            str(settings.BASE_DIR),
            settings.SECRET_KEY,
            "/proc",
            "docker.sock",
            "DATABASE_URL",
            "REDIS_URL",
            "password",
        ):
            self.assertNotIn(forbidden, html, forbidden)

    def test_existing_system_status_content_is_unchanged(self):
        self.login(self.superadmin)
        response = self.dashboard()
        for text in ("System status", "AI providers", "Background jobs", "Application", "Database"):
            self.assertContains(response, text)
        self.assertEqual(len(response.context["system_status"]["cards"]), 4)
