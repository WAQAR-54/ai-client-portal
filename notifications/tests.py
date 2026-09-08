from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Team, User
from chat.views import _notify_if_usage_warning
from governance.models import Plan, UserPlanAssignment
from governance.plans import assign_plan
from notifications.models import EmailLog, EmailSettings, Notification, NotificationPreference, NotificationType
from notifications.notify import notify, recently_notified
from notifications.tasks import sweep_expiring_demo_plans


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class NotifyFunctionTests(TestCase):
    """Unit tests of notify() itself - the single entry point every trigger
    in the codebase goes through (see notifications/notify.py)."""

    def setUp(self):
        self.user = User.objects.create_user(email="notify@example.com", password="pw12345!")

    def test_notify_creates_in_app_row(self):
        notification = notify(self.user, NotificationType.USAGE_WARNING, title="Test title", body="Test body")
        self.assertTrue(Notification.objects.filter(id=notification.id).exists())
        self.assertEqual(notification.title, "Test title")
        self.assertFalse(notification.is_read)

    def test_notify_increments_unread_count(self):
        self.assertEqual(Notification.objects.filter(user=self.user, is_read=False).count(), 0)
        notify(self.user, NotificationType.USAGE_WARNING, title="One")
        notify(self.user, NotificationType.ADMIN_CHANGE, title="Two")
        self.assertEqual(Notification.objects.filter(user=self.user, is_read=False).count(), 2)

    def test_notify_sends_email_when_no_preference_row_exists(self):
        # No NotificationPreference row at all -> "email everything" default.
        mail.outbox = []
        notify(self.user, NotificationType.USAGE_WARNING, title="Approaching limit", body="85% used")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Approaching limit", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, [self.user.email])

    def test_notify_respects_email_opt_out(self):
        NotificationPreference.objects.create(user=self.user, email_usage_warning=False)
        mail.outbox = []
        notification = notify(self.user, NotificationType.USAGE_WARNING, title="Approaching limit")
        self.assertEqual(len(mail.outbox), 0)
        # In-app row is still created even when the email is opted out of.
        self.assertTrue(Notification.objects.filter(id=notification.id).exists())

    def test_notify_marks_email_sent_flag(self):
        notification = notify(self.user, NotificationType.USAGE_WARNING, title="Approaching limit")
        notification.refresh_from_db()
        self.assertTrue(notification.email_sent)

    def test_notify_skips_email_for_user_with_no_email(self):
        # Defensive: notify() must not crash or queue mail for a user record
        # somehow missing an email address.
        self.user.email = ""
        self.user.save(update_fields=["email"])
        mail.outbox = []
        notify(self.user, NotificationType.USAGE_WARNING, title="Approaching limit")
        self.assertEqual(len(mail.outbox), 0)

    def test_recently_notified_dedup_helper(self):
        since = timezone.now() - timezone.timedelta(hours=1)
        self.assertFalse(recently_notified(self.user, NotificationType.USAGE_WARNING, since=since))
        notify(self.user, NotificationType.USAGE_WARNING, title="Approaching limit")
        self.assertTrue(recently_notified(self.user, NotificationType.USAGE_WARNING, since=since))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class UsageWarningTriggerTests(TestCase):
    """chat/views.py::_notify_if_usage_warning - fires once per 24h when a
    user crosses 80% of any cap."""

    def setUp(self):
        self.user = User.objects.create_user(email="usagewarn@example.com", password="pw12345!")

    @patch("chat.views.get_usage_status")
    def test_fires_when_usage_crosses_warn_threshold(self, mock_status):
        mock_status.return_value = {
            "has_limits": True,
            "warn": True,
            "metrics": [{"label": "Tokens today", "pct": 92}],
        }
        mail.outbox = []
        _notify_if_usage_warning(self.user)

        notification = Notification.objects.filter(user=self.user, notification_type=NotificationType.USAGE_WARNING)
        self.assertEqual(notification.count(), 1)
        self.assertIn("92%", notification.first().body)
        self.assertEqual(len(mail.outbox), 1)

    @patch("chat.views.get_usage_status")
    def test_does_not_fire_below_warn_threshold(self, mock_status):
        mock_status.return_value = {"has_limits": True, "warn": False, "metrics": []}
        _notify_if_usage_warning(self.user)
        self.assertEqual(
            Notification.objects.filter(user=self.user, notification_type=NotificationType.USAGE_WARNING).count(), 0
        )

    @patch("chat.views.get_usage_status")
    def test_does_not_refire_within_24_hours(self, mock_status):
        mock_status.return_value = {
            "has_limits": True,
            "warn": True,
            "metrics": [{"label": "Tokens today", "pct": 85}],
        }
        _notify_if_usage_warning(self.user)
        _notify_if_usage_warning(self.user)
        self.assertEqual(
            Notification.objects.filter(user=self.user, notification_type=NotificationType.USAGE_WARNING).count(), 1
        )

    @patch("chat.views.get_usage_status")
    def test_picks_the_worst_metric_when_multiple_are_over(self, mock_status):
        mock_status.return_value = {
            "has_limits": True,
            "warn": True,
            "metrics": [
                {"label": "Tokens today", "pct": 81},
                {"label": "Budget this month", "pct": 97},
            ],
        }
        _notify_if_usage_warning(self.user)
        notification = Notification.objects.get(user=self.user, notification_type=NotificationType.USAGE_WARNING)
        self.assertIn("Budget this month", notification.body)
        self.assertIn("97%", notification.body)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AdminChangeNotificationTests(TestCase):
    """The admin-changed-your-account trigger, fired from the real
    governance views (not called directly) - governance/views.py's
    _notify_admin_change / _notify_plan_change."""

    def setUp(self):
        self.department = Department.objects.create(name="Ops")
        self.admin = User.objects.create_user(
            email="notifyadmin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            department=self.department,
            is_staff=True,
        )
        self.target = User.objects.create_user(
            email="notifytarget@example.com", password="pw12345!", department=self.department
        )
        self.client.login(email="notifyadmin@example.com", password="pw12345!")

    def test_role_change_fires_admin_change_notification(self):
        team = Team.objects.create(name="Alpha", department=self.department)
        mail.outbox = []
        response = self.client.post(
            reverse("governance:change_user_role", kwargs={"user_id": self.target.id}),
            {"role": User.Role.MANAGER, "team_id": team.id, "confirmed": "1"},
        )
        self.assertEqual(response.status_code, 302)

        notification = Notification.objects.filter(
            user=self.target, notification_type=NotificationType.ADMIN_CHANGE
        ).first()
        self.assertIsNotNone(notification)
        self.assertIn("Manager", notification.body)
        self.assertEqual(len(mail.outbox), 1)

    def test_plan_change_fires_plan_change_notification(self):
        plan = Plan.objects.create(name="NotifyTestPlan", is_active=True)
        mail.outbox = []
        response = self.client.post(
            reverse("governance:change_user_plan", kwargs={"user_id": self.target.id}),
            {"plan_id": plan.id, "confirmed": "1"},
        )
        self.assertEqual(response.status_code, 302)

        notification = Notification.objects.filter(
            user=self.target, notification_type=NotificationType.PLAN_CHANGE
        ).first()
        self.assertIsNotNone(notification)
        self.assertIn("NotifyTestPlan", notification.body)
        self.assertEqual(len(mail.outbox), 1)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TrialExpiryTriggerTests(TestCase):
    """notifications/tasks.py::sweep_expiring_demo_plans - the daily Celery
    beat task. Not previously covered by any test (this file didn't exist)."""

    def setUp(self):
        self.user = User.objects.create_user(email="trialuser@example.com", password="pw12345!")
        self.plan = Plan.objects.create(
            name="TrialSweepPlan", is_demo=True, demo_duration_days=7, monthly_token_limit=1000, is_active=True
        )

    def test_fires_trial_expiring_notification_within_notice_window(self):
        assign_plan(self.user, self.plan)
        assignment = UserPlanAssignment.objects.get(user=self.user)
        # Due in 1 day - inside the default 2-day notice window.
        assignment.expires_at = timezone.now() + timezone.timedelta(days=1)
        assignment.save(update_fields=["expires_at"])

        mail.outbox = []
        result = sweep_expiring_demo_plans()

        self.assertEqual(result["expiring"], 1)
        notification = Notification.objects.filter(
            user=self.user, notification_type=NotificationType.TRIAL_EXPIRING
        ).first()
        self.assertIsNotNone(notification)
        self.assertIn("day", notification.body)
        self.assertEqual(len(mail.outbox), 1)

    def test_does_not_fire_when_expiry_is_far_away(self):
        assign_plan(self.user, self.plan)
        assignment = UserPlanAssignment.objects.get(user=self.user)
        assignment.expires_at = timezone.now() + timezone.timedelta(days=6)
        assignment.save(update_fields=["expires_at"])

        result = sweep_expiring_demo_plans()

        self.assertEqual(result["expiring"], 0)
        self.assertFalse(
            Notification.objects.filter(user=self.user, notification_type=NotificationType.TRIAL_EXPIRING).exists()
        )

    def test_fires_trial_expired_notification_after_expiry(self):
        assign_plan(self.user, self.plan)
        assignment = UserPlanAssignment.objects.get(user=self.user)
        assignment.expires_at = timezone.now() - timezone.timedelta(days=1)
        assignment.save(update_fields=["expires_at"])

        mail.outbox = []
        result = sweep_expiring_demo_plans()

        self.assertEqual(result["expired"], 1)
        notification = Notification.objects.filter(
            user=self.user, notification_type=NotificationType.TRIAL_EXPIRED
        ).first()
        self.assertIsNotNone(notification)
        self.assertEqual(len(mail.outbox), 1)

    def test_does_not_refire_expiring_notice_on_second_sweep(self):
        assign_plan(self.user, self.plan)
        assignment = UserPlanAssignment.objects.get(user=self.user)
        assignment.expires_at = timezone.now() + timezone.timedelta(days=1)
        assignment.save(update_fields=["expires_at"])

        first = sweep_expiring_demo_plans()
        second = sweep_expiring_demo_plans()

        self.assertEqual(first["expiring"], 1)
        self.assertEqual(second["expiring"], 0)
        self.assertEqual(
            Notification.objects.filter(user=self.user, notification_type=NotificationType.TRIAL_EXPIRING).count(), 1
        )

    def test_does_not_refire_expired_notice_on_second_sweep(self):
        assign_plan(self.user, self.plan)
        assignment = UserPlanAssignment.objects.get(user=self.user)
        assignment.expires_at = timezone.now() - timezone.timedelta(days=1)
        assignment.save(update_fields=["expires_at"])

        first = sweep_expiring_demo_plans()
        second = sweep_expiring_demo_plans()

        self.assertEqual(first["expired"], 1)
        self.assertEqual(second["expired"], 0)

    def test_non_demo_plan_assignment_is_never_swept(self):
        standard_plan = Plan.objects.create(name="TrialSweepStandard", is_demo=False, is_active=True)
        assign_plan(self.user, standard_plan)
        result = sweep_expiring_demo_plans()
        self.assertEqual(result["expiring"], 0)
        self.assertEqual(result["expired"], 0)


class BellDropdownAndPreferencesTests(TestCase):
    """The in-app bell UI and the Settings toggles for email-vs-in-app-only
    per notification type."""

    def setUp(self):
        self.user = User.objects.create_user(email="bellcheck@example.com", password="pw12345!")
        self.client.login(email="bellcheck@example.com", password="pw12345!")

    def test_bell_dropdown_shows_unread_notifications(self):
        notify(self.user, NotificationType.USAGE_WARNING, title="Approaching limit", body="90% used")
        response = self.client.get(reverse("notifications:bell_dropdown"))
        self.assertContains(response, "Approaching limit")

    def test_mark_all_read_clears_unread_count(self):
        notify(self.user, NotificationType.USAGE_WARNING, title="One")
        notify(self.user, NotificationType.ADMIN_CHANGE, title="Two")
        self.assertEqual(Notification.objects.filter(user=self.user, is_read=False).count(), 2)

        response = self.client.post(reverse("notifications:mark_all_read"))
        self.assertIn(response.status_code, (200, 204, 302))
        self.assertEqual(Notification.objects.filter(user=self.user, is_read=False).count(), 0)

    def test_update_preferences_persists_email_opt_out(self):
        response = self.client.post(
            reverse("notifications:update_preferences"),
            {"email_usage_warning": ""},  # unchecked checkbox = absent from POST data
        )
        self.assertEqual(response.status_code, 302)
        preference = NotificationPreference.objects.get(user=self.user)
        self.assertFalse(preference.email_usage_warning)


class EmailSettingsModelTests(TestCase):
    def test_load_creates_singleton(self):
        first = EmailSettings.load()
        second = EmailSettings.load()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(EmailSettings.objects.count(), 1)

    def test_password_round_trips_through_encryption(self):
        settings_row = EmailSettings.load()
        settings_row.set_password("super-secret-pw")
        settings_row.save()
        settings_row.refresh_from_db()
        self.assertNotIn(b"super-secret-pw", bytes(settings_row.password_encrypted))
        self.assertEqual(settings_row.get_password(), "super-secret-pw")

    def test_empty_password_stores_nothing(self):
        settings_row = EmailSettings.load()
        settings_row.set_password("")
        self.assertEqual(settings_row.password_encrypted, b"")
        self.assertEqual(settings_row.get_password(), "")

    def test_is_configured_requires_host_and_username(self):
        settings_row = EmailSettings.load()
        self.assertFalse(settings_row.is_configured())
        settings_row.host = "smtp.example.com"
        self.assertFalse(settings_row.is_configured())
        settings_row.username = "noreply@example.com"
        self.assertTrue(settings_row.is_configured())


class SendTrackedEmailTests(TestCase):
    """notifications/emailing.py - the wrapper every real send in the app
    goes through."""

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_falls_back_to_django_settings_when_unconfigured(self):
        from notifications.emailing import send_tracked_email

        # No EmailSettings configured - must still actually send (via
        # Django's own EMAIL_BACKEND), not silently no-op. This is the
        # exact regression this test guards: an existing deployment that
        # already had EMAIL_HOST set before this feature existed must
        # keep sending notification emails without any admin action.
        sent, error = send_tracked_email("someone@example.com", "Subject", "Body")
        self.assertTrue(sent)
        self.assertIsNone(error)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["someone@example.com"])

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_creates_email_log_on_success(self):
        from notifications.emailing import send_tracked_email

        send_tracked_email("someone@example.com", "Hello", "Body text")
        log = EmailLog.objects.get()
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertEqual(log.recipient, "someone@example.com")

    def test_uses_configured_settings_when_present(self):
        from notifications.emailing import send_tracked_email

        settings_row = EmailSettings.load()
        settings_row.host = "smtp.example.com"
        settings_row.username = "noreply@example.com"
        settings_row.save()

        with patch("notifications.emailing.build_connection") as mock_build:
            mock_connection = mock_build.return_value
            mock_connection.send_messages = lambda msgs: 1
            sent, error = send_tracked_email("someone@example.com", "Subject", "Body")

        mock_build.assert_called_once_with("smtp.example.com", 587, "noreply@example.com", "", "tls")
        self.assertTrue(sent)

    def test_records_failure_without_raising(self):
        from notifications.emailing import send_tracked_email

        settings_row = EmailSettings.load()
        settings_row.host = "smtp.example.com"
        settings_row.username = "noreply@example.com"
        settings_row.save()

        with patch("notifications.emailing.build_connection"), patch(
            "notifications.emailing.EmailMultiAlternatives.send", side_effect=Exception("boom")
        ):
            sent, error = send_tracked_email("someone@example.com", "Subject", "Body")

        self.assertFalse(sent)
        self.assertEqual(error, "boom")
        log = EmailLog.objects.get()
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertEqual(log.error_message, "boom")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class NotificationEmailTemplateTests(TestCase):
    """Guards the email_generic.html content itself - the "Open the
    portal"/preferences links must be real absolute URLs, not the dead
    href="#" the template shipped with before this pass."""

    def setUp(self):
        self.user = User.objects.create_user(email="template@example.com", password="pw12345!")

    def test_cta_link_is_a_real_absolute_url_not_a_dead_anchor(self):
        mail.outbox = []
        notify(self.user, NotificationType.USAGE_WARNING, title="Approaching limit", body="85% used")
        html_body = mail.outbox[0].alternatives[0][0]
        self.assertNotIn('href="#"', html_body)
        self.assertIn(f"{reverse('chat:chat_home')}\"", html_body)
        self.assertIn(f"{reverse('accounts:profile')}\"", html_body)

    def test_shows_a_human_readable_type_label(self):
        mail.outbox = []
        notify(self.user, NotificationType.TRIAL_EXPIRED, title="Your trial has ended", body="...")
        html_body = mail.outbox[0].alternatives[0][0]
        self.assertIn("Trial expired", html_body)

    def test_usage_warning_renders_metadata_driven_progress_bar(self):
        mail.outbox = []
        notify(
            self.user,
            NotificationType.USAGE_WARNING,
            title="Approaching a limit",
            body="...",
            metadata={"metric_label": "Monthly tokens", "metric_pct": 87},
        )
        html_body = mail.outbox[0].alternatives[0][0]
        self.assertIn("Monthly tokens", html_body)
        self.assertIn("87%", html_body)

    def test_plan_change_renders_plan_name_card(self):
        mail.outbox = []
        notify(
            self.user,
            NotificationType.PLAN_CHANGE,
            title="Your plan has changed",
            body="...",
            metadata={"plan_name": "Advanced"},
        )
        html_body = mail.outbox[0].alternatives[0][0]
        self.assertIn("Advanced", html_body)

    def test_falls_back_gracefully_when_metadata_is_missing(self):
        # Older/other notify() calls that don't pass metadata must not
        # crash the per-type template - it degrades to plain title/body.
        mail.outbox = []
        notify(self.user, NotificationType.USAGE_WARNING, title="Approaching a limit", body="Plain body text")
        html_body = mail.outbox[0].alternatives[0][0]
        self.assertIn("Plain body text", html_body)

    def test_shell_template_comment_never_leaks_into_a_real_email(self):
        """Regression guard: Django's {# #} comment tag does NOT support
        spanning multiple lines - a comment written across several lines
        with that syntax is not recognized as a comment at all and renders
        as literal visible text. _email_shell.html's own explanatory
        comment did exactly this and was genuinely emailed to a real
        recipient before being converted to {% comment %}...{% endcomment %}
        (which does support multiple lines)."""
        mail.outbox = []
        notify(self.user, NotificationType.PLAN_CHANGE, title="Your plan has changed", body="...")
        html_body = mail.outbox[0].alternatives[0][0]
        self.assertNotIn("{#", html_body)
        self.assertNotIn("Shared chrome for every transactional", html_body)


class TrackEmailOpenViewTests(TestCase):
    def test_pixel_marks_opened_once(self):
        log = EmailLog.objects.create(recipient="a@example.com", subject="s", status=EmailLog.Status.SENT)
        self.assertIsNone(log.opened_at)

        response = self.client.get(reverse("notifications:track_email_open", kwargs={"token": log.tracking_token}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/gif")
        log.refresh_from_db()
        first_opened_at = log.opened_at
        self.assertIsNotNone(first_opened_at)

        self.client.get(reverse("notifications:track_email_open", kwargs={"token": log.tracking_token}))
        log.refresh_from_db()
        self.assertEqual(log.opened_at, first_opened_at)

    def test_unknown_token_still_returns_pixel(self):
        import uuid

        response = self.client.get(reverse("notifications:track_email_open", kwargs={"token": uuid.uuid4()}))
        self.assertEqual(response.status_code, 200)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SendDeployNotificationCommandTests(TestCase):
    """notifications/management/commands/send_deploy_notification.py - called
    from the deploy job's SSH steps (see .github/workflows/ci.yml)."""

    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.other_superadmin = User.objects.create_user(
            email="super2@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True
        )
        mail.outbox = []

    def test_success_notifies_every_active_superadmin_only(self):
        from django.core.management import call_command

        call_command("send_deploy_notification", "--status", "success", "--sha", "abc123def456")
        self.assertEqual(len(mail.outbox), 2)
        recipients = {m.to[0] for m in mail.outbox}
        self.assertEqual(recipients, {"super@example.com", "super2@example.com"})
        self.assertIn("succeeded", mail.outbox[0].subject)
        self.assertIn("abc123def456", mail.outbox[0].subject)

    def test_inactive_superadmin_not_notified(self):
        from django.core.management import call_command

        self.other_superadmin.is_active = False
        self.other_superadmin.save(update_fields=["is_active"])

        call_command("send_deploy_notification", "--status", "success", "--sha", "abc123def456")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["super@example.com"])

    def test_failure_mentions_rollback_target(self):
        from django.core.management import call_command

        call_command(
            "send_deploy_notification",
            "--status",
            "failure",
            "--sha",
            "abc123def456",
            "--prev-sha",
            "999888777666",
        )
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("FAILED", mail.outbox[0].subject)
        self.assertIn("999888777666", mail.outbox[0].body)

    def test_no_superadmin_does_not_error(self):
        from django.core.management import call_command

        User.objects.filter(role=User.Role.SUPERADMIN).delete()
        call_command("send_deploy_notification", "--status", "success", "--sha", "abc123def456")
        self.assertEqual(len(mail.outbox), 0)
