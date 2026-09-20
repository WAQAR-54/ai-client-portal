"""Private uploads must never be reachable by a guessable /media/ URL, and the docs route
must only serve the public guides.

Regression for: config/urls.py served ALL of MEDIA_ROOT (and all of docs/) to anonymous
visitors. Chat attachment paths are predictable - chat_attachments/user_<id>/<yyyy>/<mm>/<the
user's own filename> - so a private document could be fetched without logging in."""

import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, User
from billing.models import Invoice
from chat.models import Conversation, Message
from governance.models import Plan

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82"
)


class MediaPrivacyTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)

        make = User.objects.create_user
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")
        self.owner = make(email="owner@example.com", password="pw12345!", department=self.dept_a)
        self.other = make(email="other@example.com", password="pw12345!", department=self.dept_a)
        self.admin_a = make(
            email="admin-a@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.dept_a
        )
        self.admin_b = make(
            email="admin-b@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.dept_b
        )
        self.superadmin = make(email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN)

        plan = Plan.objects.get(name="Premium")
        self.invoice = Invoice.objects.create(
            department=self.dept_a,
            recipient_user=self.owner,
            plan=plan,
            issue_date=timezone.localdate() - timedelta(days=14),
            due_date=timezone.localdate(),
            currency="USD",
            subtotal=Decimal("50"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("50"),
            status=Invoice.Status.PENDING_VERIFICATION,
        )
        self.invoice.submitted_proof_image.save("proof.png", ContentFile(PNG), save=True)

        conversation = Conversation.objects.create(user=self.owner)
        self.message = Message.objects.create(conversation=conversation, role=Message.Role.USER, content="private")
        self.message.attachment.save("contract.pdf", ContentFile(b"%PDF-1.4 private contract"), save=True)
        self.message.attachment_original_name = "contract.pdf"
        self.message.save()
        (__import__("pathlib").Path(self.media) / "branding").mkdir(exist_ok=True)
        (__import__("pathlib").Path(self.media) / "branding" / "logo.png").write_bytes(PNG)

    # ---- /media/ ----------------------------------------------------------------
    def test_a_private_upload_is_not_served_from_media_to_anyone(self):
        """Even to the exact person who owns it, and even to a SuperAdmin: the only way to
        read these is the authenticated views."""
        urls = ["/media/" + self.message.attachment.name, "/media/" + self.invoice.submitted_proof_image.name]
        for who in (None, self.owner, self.other, self.admin_a, self.superadmin):
            if who:
                self.client.force_login(who)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 404, (who and who.email, url))
            self.client.logout()

    def test_branding_stays_public_because_the_login_page_needs_it(self):
        response = self.client.get("/media/branding/logo.png")
        self.assertEqual(response.status_code, 200)

    def test_traversal_out_of_branding_does_not_reach_private_files(self):
        for url in (
            "/media/branding/../chat_attachments/x",
            "/media/branding/%2e%2e/invoice_proofs/x",
            "/media/branding/..%2f" + self.message.attachment.name.replace("/", "%2f"),
        ):
            self.assertIn(self.client.get(url).status_code, (400, 404), url)

    # ---- the authenticated way to see a payment proof ----------------------------
    def _proof(self, who):
        client = self.client
        client.logout()
        if who:
            client.force_login(who)
        return client.get(reverse("billing:invoice_proof", args=[self.invoice.id]))

    def test_the_recipient_and_the_departments_admin_and_a_superadmin_can_open_the_proof(self):
        for who in (self.owner, self.admin_a, self.superadmin):
            response = self._proof(who)
            self.assertEqual(response.status_code, 200, who.email)
            self.assertEqual(b"".join(response.streaming_content), PNG)
            self.assertEqual(response["Content-Type"], "image/png")
            self.assertIn("no-store", response["Cache-Control"])

    def test_nobody_else_can_open_it(self):
        for who in (self.other, self.admin_b):
            self.assertEqual(self._proof(who).status_code, 403, who.email)
        response = self._proof(None)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_an_invoice_without_a_proof_is_a_404_not_an_error(self):
        self.invoice.submitted_proof_image.delete(save=True)
        self.assertEqual(self._proof(self.owner).status_code, 404)

    def test_the_pages_link_to_the_authenticated_view_not_to_media(self):
        proof_url = reverse("billing:invoice_proof", args=[self.invoice.id])
        self.client.force_login(self.admin_a)
        detail = self.client.get(reverse("billing:invoice_detail", args=[self.invoice.id])).content.decode()
        self.assertIn(proof_url, detail)
        self.assertNotIn("/media/invoice_proofs", detail)
        listing = self.client.get(reverse("billing:invoices")).content.decode()
        self.assertIn(proof_url, listing)
        self.assertNotIn("/media/invoice_proofs", listing)

    # ---- the chat download view still works for the owner only -------------------
    def test_the_attachment_download_view_still_serves_the_owner_and_nobody_else(self):
        url = reverse(
            "chat:download_attachment",
            kwargs={"conversation_id": self.message.conversation_id, "message_id": self.message.id},
        )
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 200)
        for who in (self.other, self.admin_a, self.superadmin):
            self.client.force_login(who)
            self.assertIn(self.client.get(url).status_code, (403, 404), who.email)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)


class PublicDocsTests(TestCase):
    def test_the_public_guides_are_served(self):
        for url in ("/docs/guides/index.html", "/docs/guides/user.html", "/docs/FEATURE_GUIDE.html"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_operational_notes_are_never_served(self):
        for name in ("SECRETS.md", "PRODUCTION_ACCESS.md", "LOCAL_ACCESS.md", "BACKUP_RESTORE.md"):
            self.assertEqual(self.client.get(f"/docs/{name}").status_code, 404, name)

    def test_only_html_guides_pass_and_traversal_does_not(self):
        for url in (
            "/docs/guides/",  # redirect target only
            "/docs/../config/settings.py",
            "/docs/guides/../SECRETS.md",
            "/docs/guides/x.md",
            "/docs/guides/sub/x.html",
            "/docs/.env",
        ):
            self.assertNotEqual(self.client.get(url).status_code, 200, url)
