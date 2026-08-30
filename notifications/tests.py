from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from chat.views import _notify_if_usage_warning
from governance.models import Plan, UserPlanAssignment
from governance.plans import assign_plan
from notifications.models import Notification, NotificationPreference, NotificationType
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
        self.admin = User.objects.create_user(
            email="notifyadmin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True
        )
        self.target = User.objects.create_user(email="notifytarget@example.com", password="pw12345!")
        self.client.login(email="notifyadmin@example.com", password="pw12345!")

    def test_role_change_fires_admin_change_notification(self):
        mail.outbox = []
        response = self.client.post(
            reverse("governance:change_user_role", kwargs={"user_id": self.target.id}),
            {"role": User.Role.MANAGER},
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
