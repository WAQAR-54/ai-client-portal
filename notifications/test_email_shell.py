"""Every email the server sends is rendered from the global email shell (templates/notifications/_email_shell.html).

Each real sender is exercised below and its outgoing message must carry the shell marker and the active branding. The
guarantee itself lives in notifications/emailing.py::send_via_connection: a caller that passes no HTML gets the shell
wrapped around its text, so a future sender cannot leak a bare-text email by forgetting to render a template."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from accounts.tasks import send_mfa_code_email_task, send_password_reset_email_task
from billing.emails import send_invoice_email, send_overdue_reminder_email
from billing.models import Invoice, Plan
from governance.models import SiteBranding
from notifications.emailing import render_shell_email, send_tracked_email
from notifications.models import NotificationType
from notifications.notify import notify
from notifications.tasks import send_admin_error_alert

MARKER = 'data-email-shell="global"'


def html_of(message):
    return message.alternatives[0][0] if message.alternatives else ""


class EmailShellEverywhereTests(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox = []
        self.superadmin = User.objects.create_user(
            email="root@corp-mail.io", password="pw12345!Strong", role=User.Role.SUPERADMIN
        )
        self.user = User.objects.create_user(email="user@corp-mail.io", password="pw12345!Strong")

    def assertShell(self, message):
        html = html_of(message)
        self.assertIn(MARKER, html, f"{message.subject!r} was not rendered from the global email shell")
        self.assertIn("<table", html)

    def test_a_bare_text_send_is_wrapped_in_the_shell(self):
        sent, _error = send_tracked_email("someone@corp-mail.io", "[Portal] Anything", "Line one\nLine two <b>x</b>")
        self.assertTrue(sent)
        message = mail.outbox[0]
        self.assertShell(message)
        self.assertEqual(message.body, "Line one\nLine two <b>x</b>")  # the plain-text part stays plain
        self.assertIn("Line two &lt;b&gt;x&lt;/b&gt;", html_of(message))  # text is escaped, never markup
        self.assertIn("Anything", html_of(message))  # the "[Portal]" prefix is not repeated as the heading

    def test_deploy_notification_uses_the_shell(self):
        call_command("send_deploy_notification", "--status", "success", "--sha", "abcdef1234567", stdout=StringIO())
        call_command("send_deploy_notification", "--status", "failure", "--sha", "abcdef1234567", stdout=StringIO())
        deploys = [m for m in mail.outbox if "Deploy" in m.subject]
        self.assertEqual(len(deploys), 2)
        for message in deploys:
            self.assertShell(message)

    def test_crash_alert_uses_the_shell(self):
        send_admin_error_alert("[Portal] Internal Server Error: /x/", "Traceback ...\nBoom")
        self.assertEqual(len(mail.outbox), 1)
        self.assertShell(mail.outbox[0])
        self.assertIn("Boom", html_of(mail.outbox[0]))

    def test_smtp_test_email_uses_the_shell(self):
        self.client.force_login(self.superadmin)
        with patch("notifications.emailing.build_connection", return_value=mail.get_connection()):
            self.client.post(
                reverse("governance:send_test_email"),
                {"host": "smtp.test", "username": "u", "test_email": "me@corp-mail.io", "password": "x"},
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertShell(mail.outbox[0])

    def test_notification_email_uses_the_shell(self):
        notify(self.user, NotificationType.PLAN_CHANGE, "Plan changed", "You are on Growth", metadata={})
        self.assertShell(mail.outbox[0])

    def test_mfa_and_password_reset_emails_use_the_shell(self):
        send_mfa_code_email_task(self.user.pk, "123456")
        send_password_reset_email_task(self.user.pk, "uid", "token")
        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            self.assertShell(message)

    def test_invoice_and_overdue_emails_use_the_shell(self):
        plan = Plan.objects.create(name="Growth")
        invoice = Invoice.objects.create(
            department=None,
            recipient_user=self.user,
            plan=plan,
            issue_date=timezone.localdate() - timedelta(days=40),
            due_date=timezone.localdate() - timedelta(days=10),
            currency="USD",
            subtotal=Decimal("50"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("50"),
            status=Invoice.Status.UNPAID,
        )
        with patch("billing.emails._pdf_attachment", return_value=("i.pdf", b"%PDF", "application/pdf")):
            send_invoice_email(invoice)
            send_overdue_reminder_email(invoice)
        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            self.assertShell(message)

    def test_the_shell_follows_the_active_branding(self):
        SiteBranding.objects.update_or_create(pk=1, defaults={"preset": "branding_2", "version": 500})
        cache.clear()
        html = render_shell_email("[Portal] Hello", "Body text")
        self.assertIn(MARKER, html)
        self.assertIn("gilroy", html.lower())  # Branding 2's font
        self.assertIn("whe-logo-blue.png", html)  # Branding 2's logo
        SiteBranding.objects.update_or_create(pk=1, defaults={"preset": "branding_1", "version": 501})
        cache.clear()
        self.assertNotIn("gilroy", render_shell_email("[Portal] Hello", "Body text").lower())

    def test_a_shell_failure_never_stops_the_email(self):
        with patch("notifications.emailing.render_shell_email", side_effect=RuntimeError("template broke")):
            sent, _error = send_tracked_email("someone@corp-mail.io", "[Portal] Anything", "Plain body")
        self.assertTrue(sent)
        self.assertEqual(mail.outbox[0].body, "Plain body")
