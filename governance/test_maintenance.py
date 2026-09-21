"""Maintenance Mode: the rules (governance/maintenance.py), the request-level enforcement (governance/middleware.py),
the SuperAdmin control page, the branded page visitors see, the audit trail and the emails."""

from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from governance import maintenance
from governance.models import AuditLog, MaintenanceWindow, SiteBranding
from notifications.models import Notification

PASSWORD = "pw12345!Strong"
Status = MaintenanceWindow.Status


def hours(n):
    return timezone.now() + timedelta(hours=n)


class MaintenanceTestCase(TestCase):
    def setUp(self):
        cache.clear()
        # TestCase never commits, so on_commit callbacks (the queued emails) would never run: run them straight away.
        patcher = patch.object(maintenance.transaction, "on_commit", lambda callback, *args, **kwargs: callback())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.superadmin = User.objects.create_user(email="root@corp.io", password=PASSWORD, role=User.Role.SUPERADMIN)
        self.other_super = User.objects.create_user(email="root2@corp.io", password=PASSWORD, role=User.Role.SUPERADMIN)
        self.admin = User.objects.create_user(email="admin@corp.io", password=PASSWORD, role=User.Role.ADMIN)
        self.manager = User.objects.create_user(email="mgr@corp.io", password=PASSWORD, role=User.Role.MANAGER)
        self.user = User.objects.create_user(email="user@corp.io", password=PASSWORD)

    def audit(self, action):
        return AuditLog.objects.filter(action_type=action)


class RulesTests(MaintenanceTestCase):
    def test_create_a_scheduled_window(self):
        window = maintenance.schedule(self.superadmin, "Upgrade", "Back soon", hours(2), hours(3))
        self.assertEqual((window.status, window.kind, window.open_slot), (Status.SCHEDULED, "scheduled", True))
        self.assertEqual(maintenance.open_window(), window)
        self.assertIsNone(maintenance.current_state(), "a scheduled window does not block anyone yet")

    def test_activate_immediately(self):
        window = maintenance.enable_now(self.superadmin, "Hotfix", "", end=None)
        self.assertEqual((window.status, window.kind), (Status.ACTIVE, "immediate"))
        self.assertIsNotNone(window.actual_start)
        self.assertEqual(maintenance.current_state()["reason"], "Hotfix")

    def test_scheduled_becomes_active_when_its_time_comes(self):
        window = maintenance.schedule(self.superadmin, "Upgrade", "", hours(1), hours(2))
        maintenance.advance(timezone.now() + timedelta(hours=1, minutes=1))
        window.refresh_from_db()
        self.assertEqual(window.status, Status.ACTIVE)
        self.assertIsNotNone(window.actual_start)
        self.assertTrue(
            self.audit("maintenance.enabled").filter(actor__isnull=True).exists(), "automatic start is audited"
        )

    def test_active_becomes_completed_at_its_end(self):
        window = maintenance.enable_now(self.superadmin, "Hotfix", "", end=hours(1))
        maintenance.advance(timezone.now() + timedelta(hours=1, minutes=1))
        window.refresh_from_db()
        self.assertEqual((window.status, window.open_slot), (Status.COMPLETED, None))
        self.assertIsNotNone(window.actual_end)
        self.assertIsNone(window.closed_by, "an automatic end has no person behind it")
        self.assertTrue(self.audit("maintenance.completed").exists())
        self.assertIsNone(maintenance.open_window())

    def test_scheduled_can_be_cancelled(self):
        window = maintenance.schedule(self.superadmin, "Upgrade", "", hours(1), hours(2))
        maintenance.cancel(self.superadmin, window.pk)
        window.refresh_from_db()
        self.assertEqual((window.status, window.open_slot, window.closed_by), (Status.CANCELLED, None, self.superadmin))
        self.assertTrue(self.audit("maintenance.cancelled").exists())

    def test_active_can_be_ended_by_hand(self):
        window = maintenance.enable_now(self.superadmin, "Hotfix", "")
        maintenance.end(self.other_super, window.pk)
        window.refresh_from_db()
        self.assertEqual((window.status, window.closed_by), (Status.COMPLETED, self.other_super))
        self.assertTrue(self.audit("maintenance.ended").exists())
        self.assertIsNone(maintenance.current_state())

    def test_a_window_that_went_by_unseen_is_cancelled_not_started(self):
        window = maintenance.schedule(self.superadmin, "Upgrade", "", hours(1), hours(2))
        maintenance.advance(timezone.now() + timedelta(hours=5))
        window.refresh_from_db()
        self.assertEqual(window.status, Status.CANCELLED)
        self.assertIsNone(window.actual_start)

    def test_invalid_time_ranges_are_refused(self):
        bad = [
            (hours(-1), hours(1)),  # starts in the past
            (hours(2), hours(1)),  # ends before it starts
            (hours(2), hours(2)),  # empty
            (hours(1), hours(1) + timedelta(days=8)),  # longer than 7 days
            (None, hours(2)),  # no start
            (hours(2), None),  # no end
        ]
        for start, end in bad:
            with self.subTest(start=start, end=end), self.assertRaises(maintenance.MaintenanceError):
                maintenance.schedule(self.superadmin, "Upgrade", "", start, end)
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.enable_now(self.superadmin, "Hotfix", "", end=hours(-1))
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.enable_now(self.superadmin, "   ", "")
        self.assertFalse(MaintenanceWindow.objects.exists(), "a refused request leaves nothing behind")

    def test_parse_local_reads_the_portal_time_zone(self):
        parsed = maintenance.parse_local("2026-09-22T14:30")
        self.assertEqual((parsed.hour, parsed.minute), (14, 30))  # settings.TIME_ZONE is UTC
        self.assertIsNone(maintenance.parse_local(""))
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.parse_local("not a date")

    def test_conflicting_windows_are_refused(self):
        maintenance.schedule(self.superadmin, "Upgrade", "", hours(1), hours(2))
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.enable_now(self.superadmin, "Hotfix", "")
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.schedule(self.superadmin, "Another", "", hours(5), hours(6))
        self.assertEqual(MaintenanceWindow.objects.count(), 1)

    def test_the_database_itself_refuses_a_second_open_window(self):
        maintenance.enable_now(self.superadmin, "Hotfix", "")
        with self.assertRaises(IntegrityError), transaction.atomic():
            MaintenanceWindow.objects.create(
                kind="immediate", status=Status.ACTIVE, reason="Racing", open_slot=True, created_by=self.superadmin
            )

    def test_closed_windows_do_not_block_a_new_one(self):
        first = maintenance.enable_now(self.superadmin, "Hotfix", "")
        maintenance.end(self.superadmin, first.pk)
        second = maintenance.enable_now(self.superadmin, "Second", "")
        self.assertEqual(second.status, Status.ACTIVE)

    def test_a_double_click_cannot_end_or_cancel_twice(self):
        window = maintenance.enable_now(self.superadmin, "Hotfix", "")
        maintenance.end(self.superadmin, window.pk)
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.end(self.superadmin, window.pk)
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.cancel(self.superadmin, window.pk)
        self.assertEqual(self.audit("maintenance.ended").count(), 1)

    def test_text_is_plain_and_capped(self):
        window = maintenance.enable_now(
            self.superadmin, "<b>Up\x00grade</b>\n now", "line one\r\nline two\x07" + "x" * 2000
        )
        self.assertEqual(
            window.reason, "<b>Upgrade</b> now"
        )  # single line, control characters gone (escaped on output)
        self.assertNotIn("\x07", window.message)
        self.assertEqual(window.message.split("\n")[0], "line one")
        self.assertLessEqual(len(window.message), maintenance.MESSAGE_MAX)


class AuditTests(MaintenanceTestCase):
    def test_every_transition_is_audited_with_the_actor(self):
        scheduled = maintenance.schedule(self.superadmin, "Upgrade", "", hours(1), hours(2))
        maintenance.cancel(self.superadmin, scheduled.pk)
        window = maintenance.enable_now(self.superadmin, "Hotfix", "")
        maintenance.end(self.superadmin, window.pk)
        actions = list(
            AuditLog.objects.filter(action_type__startswith="maintenance.").values_list("action_type", flat=True)
        )
        self.assertEqual(
            sorted(actions),
            ["maintenance.cancelled", "maintenance.enabled", "maintenance.ended", "maintenance.scheduled"],
        )
        row = self.audit("maintenance.scheduled").get()
        self.assertEqual(
            (row.actor, row.target_type, row.target_id), (self.superadmin, "MaintenanceWindow", str(scheduled.pk))
        )


@override_settings(SUPPORT_EMAIL="help@corp.io")
class EnforcementTests(MaintenanceTestCase):
    def enable(self, **kwargs):
        return maintenance.enable_now(self.superadmin, "Database upgrade", "Storage work", **kwargs)

    def test_nothing_is_blocked_while_off_or_scheduled(self):
        self.client.force_login(self.user)
        self.assertNotEqual(self.client.get(reverse("accounts:dashboard")).status_code, 503)
        maintenance.schedule(self.superadmin, "Upgrade", "", hours(1), hours(2))
        cache.clear()
        self.assertNotEqual(self.client.get(reverse("accounts:dashboard")).status_code, 503)

    def test_every_non_superadmin_sees_the_maintenance_page(self):
        self.enable()
        for who in (self.user, self.manager, self.admin):
            self.client.force_login(who)
            for url in ("/", reverse("chat:chat_home"), reverse("governance:users"), reverse("accounts:profile")):
                with self.subTest(role=who.role, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 503)
                    self.assertContains(response, 'data-page="maintenance"', status_code=503)
                    self.assertEqual(response["Cache-Control"], "no-store")
                    self.assertIn("Retry-After", response)

    def test_direct_urls_and_posts_are_enforced_too(self):
        self.enable()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(reverse("governance:add_user"), {"email": "x@corp.io"}).status_code, 503)
        self.assertFalse(User.objects.filter(email="x@corp.io").exists())
        self.assertEqual(self.client.get("/no/such/page/").status_code, 503)

    def test_anonymous_visitors_see_the_maintenance_page_without_leaking_anything(self):
        self.enable()
        response = self.client.get(reverse("chat:chat_home"))
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "Database upgrade", status_code=503)
        self.assertNotContains(response, "root@corp.io", status_code=503)
        self.assertContains(response, reverse("accounts:login"), status_code=503)  # the way in for a SuperAdmin

    def test_superadmin_keeps_normal_access(self):
        self.enable()
        self.client.force_login(self.superadmin)
        self.assertEqual(self.client.get(reverse("governance:maintenance")).status_code, 200)
        self.assertEqual(self.client.get(reverse("governance:users")).status_code, 200)
        self.assertNotEqual(self.client.get(reverse("chat:chat_home")).status_code, 503)

    def test_health_endpoints_are_never_blocked(self):
        self.enable()
        for url in ("/healthz/", "/healthz/deep/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200, url)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/healthz/").status_code, 200)

    def test_sign_in_stays_usable_and_gives_nothing_away(self):
        self.enable()
        login = self.client.get(reverse("accounts:login"))
        self.assertEqual(login.status_code, 200)
        self.assertNotContains(login, 'data-page="maintenance"')
        wrong = self.client.post(reverse("accounts:login"), {"username": "root@corp.io", "password": "wrong"})
        unknown = self.client.post(reverse("accounts:login"), {"username": "nobody@corp.io", "password": "wrong"})
        self.assertEqual((wrong.status_code, unknown.status_code), (200, 200))
        self.assertEqual(wrong.content.decode().count("error"), unknown.content.decode().count("error"))
        self.assertNotEqual(self.client.post(reverse("accounts:logout")).status_code, 503)

    def test_a_superadmin_can_sign_in_and_switch_it_off(self):
        self.enable()
        signed_in = self.client.post(reverse("accounts:login"), {"username": "root@corp.io", "password": PASSWORD})
        self.assertNotEqual(signed_in.status_code, 503)  # the sign-in itself is never blocked
        self.client.force_login(self.superadmin)  # MFA is a separate, already-tested step of sign-in
        window = maintenance.open_window()
        response = self.client.post(reverse("governance:maintenance_end", kwargs={"window_id": window.pk}))
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.user)
        self.assertNotEqual(self.client.get(reverse("chat:chat_home")).status_code, 503)

    def test_htmx_requests_are_sent_to_the_page_instead_of_being_swapped(self):
        self.enable()
        self.client.force_login(self.user)
        response = self.client.get(reverse("chat:chat_home"), HTTP_HX_REQUEST="true")
        self.assertEqual((response.status_code, response["HX-Redirect"]), (204, "/"))

    def test_the_clock_switches_it_on_and_off_without_the_beat_task(self):
        window = maintenance.schedule(self.superadmin, "Upgrade", "", hours(1), hours(2))
        self.assertNotEqual(self.client.get("/").status_code, 503)
        MaintenanceWindow.objects.filter(pk=window.pk).update(
            scheduled_start=timezone.now() - timedelta(minutes=1), scheduled_end=hours(1)
        )
        maintenance.invalidate()
        self.assertEqual(self.client.get("/").status_code, 503)
        MaintenanceWindow.objects.filter(pk=window.pk).update(scheduled_end=timezone.now() - timedelta(seconds=1))
        maintenance.invalidate()
        self.assertNotEqual(self.client.get("/").status_code, 503)
        window.refresh_from_db()
        self.assertEqual(window.status, Status.COMPLETED)

    def test_a_broken_check_leaves_the_site_open(self):
        self.enable()
        with patch.object(maintenance, "_load_state", side_effect=RuntimeError("db down")):
            cache.clear()
            self.assertIsNone(maintenance.current_state())
            self.assertNotEqual(self.client.get("/").status_code, 503)


@override_settings(SUPPORT_EMAIL="help@corp.io")
class PageTests(MaintenanceTestCase):
    def page(self, **kwargs):
        maintenance.enable_now(self.superadmin, "Database upgrade", kwargs.pop("message", "Storage work"), **kwargs)
        return self.client.get("/")

    def test_shows_reason_message_times_timezone_and_support(self):
        response = self.page(end=hours(1))
        for text in ("Database upgrade", "Storage work", "Started", "Expected end", "UTC", "help@corp.io", "Refresh"):
            self.assertContains(response, text, status_code=503)

    def test_countdown_data_and_the_window_ended_state(self):
        response = self.page(end=hours(1))
        end = int(MaintenanceWindow.objects.get().scheduled_end.timestamp())
        self.assertContains(response, f'data-end="{end}"', status_code=503)
        self.assertContains(response, "data-now=", status_code=503)
        self.assertContains(
            response, "The maintenance window has ended.", status_code=503
        )  # shown by the script at zero

    def test_no_countdown_without_an_end_time(self):
        self.assertNotContains(self.page(), 'id="countdown"', status_code=503)

    def test_message_and_reason_are_escaped_never_html(self):
        maintenance.enable_now(self.superadmin, "<script>alert(1)</script>", "<img src=x onerror=alert(2)>\nsecond")
        html = self.client.get("/").content.decode()
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;<br>second", html)

    def test_uses_the_active_branding_not_a_second_design(self):
        for preset, marker in (("branding_1", "#00aef0"), ("branding_2", "#2c6ef8"), ("branding_3", None)):
            with self.subTest(preset=preset):
                SiteBranding.objects.update_or_create(pk=1, defaults={"preset": preset, "version": 1000 + len(preset)})
                cache.clear()
                maintenance.enable_now(self.superadmin, "Upgrade", "")
                html = self.client.get("/").content.decode()
                self.assertIn(f'data-brand="{preset}"', html)
                self.assertIn('id="brand-tokens"', html)
                if marker:
                    self.assertIn(marker, html.lower())
                maintenance.end(self.superadmin, maintenance.open_window().pk)

    def test_no_hard_coded_colours_in_the_page_itself(self):
        import re
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "templates" / "maintenance.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", source), [])

    def test_preview_uses_the_same_template_and_activates_nothing(self):
        self.client.force_login(self.superadmin)
        response = self.client.get(reverse("governance:brand_maintenance_preview"))
        self.assertContains(response, 'data-page="maintenance"')
        self.assertContains(response, "Preview")
        self.assertFalse(MaintenanceWindow.objects.exists())


class ControlPageTests(MaintenanceTestCase):
    def urls(self):
        return [
            ("get", reverse("governance:maintenance")),
            ("post", reverse("governance:maintenance_enable_now")),
            ("post", reverse("governance:maintenance_schedule")),
            ("post", reverse("governance:maintenance_cancel", kwargs={"window_id": 1})),
            ("post", reverse("governance:maintenance_end", kwargs={"window_id": 1})),
        ]

    def test_only_a_superadmin_may_use_it(self):
        window = maintenance.schedule(self.superadmin, "Upgrade", "", hours(1), hours(2))
        for who in (self.user, self.manager, self.admin):
            self.client.force_login(who)
            for method, url in self.urls():
                with self.subTest(role=who.role, url=url):
                    self.assertEqual(getattr(self.client, method)(url, {"reason": "x"}).status_code, 403)
        self.client.logout()
        for method, url in self.urls():
            with self.subTest(role="anonymous", url=url):
                self.assertRedirects(
                    getattr(self.client, method)(url, {}), reverse("accounts:login"), fetch_redirect_response=False
                )
        window.refresh_from_db()
        self.assertEqual(window.status, Status.SCHEDULED, "no blocked request changed anything")
        self.assertFalse(self.audit("maintenance.enabled").exists())

    def test_the_page_shows_status_forms_and_history(self):
        self.client.force_login(self.superadmin)
        off = self.client.get(reverse("governance:maintenance"))
        self.assertContains(off, 'data-maintenance-status="off"')
        self.assertContains(off, "Enable Now")
        self.assertContains(off, "Schedule")
        self.assertContains(off, "portalConfirmSubmit")
        self.assertNotContains(off, "DELETE")
        self.client.post(reverse("governance:maintenance_schedule"), self.schedule_form())
        scheduled = self.client.get(reverse("governance:maintenance"))
        self.assertContains(scheduled, 'data-maintenance-status="scheduled"')
        self.assertContains(scheduled, "Cancel Scheduled")
        self.assertNotContains(scheduled, 'action="%s"' % reverse("governance:maintenance_enable_now"))
        self.client.post(reverse("governance:maintenance_cancel", kwargs={"window_id": maintenance.open_window().pk}))
        self.client.post(reverse("governance:maintenance_enable_now"), {"reason": "Hotfix", "notify_users": "1"})
        active = self.client.get(reverse("governance:maintenance"))
        self.assertContains(active, 'data-maintenance-status="active"')
        self.assertContains(active, "End Maintenance")
        self.assertContains(active, 'id="mt-history"')

    def schedule_form(self, **over):
        local = lambda dt: dt.strftime("%Y-%m-%dT%H:%M")  # noqa: E731
        form = {
            "reason": "Upgrade",
            "message": "Short break",
            "start": local(hours(2)),
            "end": local(hours(3)),
            "notify_users": "1",
        }
        return form | over

    def test_history_names_people_by_name_never_by_email(self):
        self.superadmin.first_name, self.superadmin.last_name = "Sam", "Root"
        self.superadmin.save()
        self.client.force_login(self.superadmin)
        self.client.post(reverse("governance:maintenance_enable_now"), {"reason": "Hotfix", "message": "PRIVATE-BODY"})
        page = self.client.get(reverse("governance:maintenance"))
        self.assertContains(page, "Sam Root")
        table = page.content.decode().split('id="mt-history"')[1].split("</table>")[0]
        self.assertNotIn("PRIVATE-BODY", table)
        self.assertNotIn("@", table, "the history shows names, never e-mail addresses")

    def test_a_bad_request_shows_a_message_not_a_server_error(self):
        self.client.force_login(self.superadmin)
        response = self.client.post(
            reverse("governance:maintenance_schedule"), self.schedule_form(end="garbage"), follow=True
        )
        self.assertContains(response, "not valid")
        response = self.client.post(
            reverse("governance:maintenance_schedule"), self.schedule_form(start="2001-01-01T00:00"), follow=True
        )
        self.assertContains(response, "must be in the future")
        self.assertContains(response, "Upgrade")  # what was typed is kept
        self.assertFalse(MaintenanceWindow.objects.exists())
        response = self.client.post(reverse("governance:maintenance_end", kwargs={"window_id": 999}), follow=True)
        self.assertEqual(response.status_code, 200)

    def test_actions_are_post_only(self):
        self.client.force_login(self.superadmin)
        for name in ("maintenance_enable_now", "maintenance_schedule"):
            self.assertEqual(self.client.get(reverse(f"governance:{name}")).status_code, 405)

    def test_the_nav_offers_it_to_superadmins_only(self):
        url = reverse("governance:maintenance")
        self.client.force_login(self.superadmin)
        self.assertContains(self.client.get(reverse("governance:dashboard")), f'href="{url}"')
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse("governance:dashboard")), f'href="{url}"')


class NoticeEmailTests(MaintenanceTestCase):
    """Emails go through notify() and the global email shell. The test runner's mail backend is in-memory, so no real
    mail is ever sent."""

    def sent_to(self, address):
        return [m for m in mail.outbox if address in m.to]

    def html(self, message):
        return message.alternatives[0][0]

    def test_scheduled_notice_reaches_every_active_user_once_in_the_global_shell(self):
        User.objects.filter(pk=self.manager.pk).update(is_active=False)
        maintenance.schedule(self.superadmin, "Database upgrade", "Storage work", hours(2), hours(3))
        self.assertEqual(len(self.sent_to("user@corp.io")), 1)
        self.assertEqual(self.sent_to("mgr@corp.io"), [], "an inactive account gets nothing")
        message = self.sent_to("user@corp.io")[0]
        html = self.html(message)
        self.assertIn('data-email-shell="global"', html)
        self.assertIn("Database upgrade", html)
        self.assertIn("UTC", html)
        self.assertIn("Scheduled maintenance", message.subject)
        self.assertEqual(Notification.objects.filter(user=self.user, notification_type="maintenance").count(), 1)

    def test_a_notice_is_never_sent_twice(self):
        window = maintenance.enable_now(self.superadmin, "Hotfix", "")
        before = len(mail.outbox)
        self.assertGreater(before, 0)
        for _ in range(3):
            self.assertEqual(maintenance.deliver_notice(window.pk, "started"), 0)
        maintenance.advance(timezone.now() + timedelta(days=1))  # nothing due: still nothing new
        self.assertEqual(len(mail.outbox), before)

    def test_full_lifecycle_sends_one_email_per_kind(self):
        window = maintenance.schedule(self.superadmin, "Upgrade", "", hours(1), hours(2))
        maintenance.advance(timezone.now() + timedelta(hours=1, minutes=1))
        maintenance.advance(timezone.now() + timedelta(hours=3))
        window.refresh_from_db()
        self.assertEqual(window.status, Status.COMPLETED)
        subjects = [m.subject for m in self.sent_to("user@corp.io")]
        self.assertEqual(len(subjects), 3, subjects)  # scheduled, started, completed
        self.assertTrue(any("Maintenance has started" in s for s in subjects))
        self.assertTrue(any("Maintenance is complete" in s for s in subjects))

    def test_cancelling_tells_people_only_if_they_were_told_about_it(self):
        told = maintenance.schedule(self.superadmin, "Upgrade", "", hours(1), hours(2))
        maintenance.cancel(self.superadmin, told.pk)
        self.assertTrue(any("cancelled" in m.subject for m in self.sent_to("user@corp.io")))
        mail.outbox.clear()
        quiet = maintenance.schedule(self.superadmin, "Quiet", "", hours(1), hours(2), notify_users=False)
        maintenance.cancel(self.superadmin, quiet.pk)
        self.assertEqual(mail.outbox, [])

    def test_email_users_can_be_switched_off(self):
        maintenance.enable_now(self.superadmin, "Hotfix", "", notify_users=False)
        self.assertEqual(mail.outbox, [])
        self.assertFalse(Notification.objects.filter(notification_type="maintenance").exists())

    def test_email_uses_the_active_branding_and_escapes_the_message(self):
        SiteBranding.objects.update_or_create(pk=1, defaults={"preset": "branding_2", "version": 77})
        cache.clear()
        maintenance.enable_now(self.superadmin, "<b>Upgrade</b>", "<script>alert(1)</script>")
        html = self.html(self.sent_to("user@corp.io")[0])
        self.assertIn('data-email-shell="global"', html)
        # Branding 2's own font, logo and text colour (the amber accent is a fixed status colour, like the other types)
        self.assertIn("gilroy", html.lower())
        self.assertIn("whe-logo-blue.png", html)
        self.assertIn("color:#151515", html)
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<b>Upgrade</b>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_recipients_read_their_own_language(self):
        User.objects.filter(pk=self.user.pk).update(preferred_language="ur")
        maintenance.enable_now(self.superadmin, "Hotfix", "")
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.admin).count(), 1)
