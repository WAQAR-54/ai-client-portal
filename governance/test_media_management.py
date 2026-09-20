"""Server Media Management: SuperAdmin-only, database-driven, never a backdoor to private files.

Everything runs against a throw-away MEDIA_ROOT with synthetic files."""

import os
import re
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from billing.models import Invoice
from chat.models import Conversation, Message
from governance import media_service as media
from governance.models import AuditLog, Plan, SiteBranding

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82"
)
SECRET_TEXT = b"SECRET-BODY-should-never-be-logged"


class MediaBase(TestCase):
    def setUp(self):
        cache.clear()
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        make = User.objects.create_user
        self.superadmin = make(email="root@example.com", password="pw12345!", role=User.Role.SUPERADMIN)
        self.admin = make(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN)
        self.manager = make(email="manager@example.com", password="pw12345!", role=User.Role.MANAGER)
        self.owner = make(email="owner@example.com", password="pw12345!")
        self.other = make(email="other@example.com", password="pw12345!")

    def login(self, user):
        self.client.force_login(user)

    def message_with_file(self, name="report.pdf", data=b"%PDF-1.4 " + SECRET_TEXT, user=None, size=True):
        conversation = Conversation.objects.create(user=user or self.owner, title="Quarterly review")
        message = Message.objects.create(conversation=conversation, role=Message.Role.USER, content="see file")
        message.attachment.save(name, ContentFile(data), save=False)
        message.attachment_original_name = name
        message.attachment_size = len(data) if size else None
        message.save()
        return message

    def write_stray(self, relative, data=b"stray"):
        path = Path(self.media_root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def stray_token(self, relative):
        return media.name_digest(relative)

    def make_invoice(self):
        invoice = Invoice.objects.create(
            department=None,
            recipient_user=self.owner,
            plan=Plan.objects.get(name="Premium"),
            issue_date=timezone.localdate(),
            due_date=timezone.localdate() + timedelta(days=14),
            currency="USD",
            subtotal=Decimal("50"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("50"),
        )
        invoice.submitted_proof_image.save("proof.png", ContentFile(PNG), save=True)
        return invoice


class MediaAccessTests(MediaBase):
    """1. unauthorized access, 2. non-superadmin admin denied, 3. authorized view."""

    def _urls(self, message):
        return [
            reverse("governance:media"),
            reverse("governance:media_download", args=["chat", message.pk]),
            reverse("governance:media_preview", args=["chat", message.pk]),
        ]

    def test_anonymous_is_sent_to_login_everywhere(self):
        message = self.message_with_file()
        for url in self._urls(message):
            self.assertEqual(self.client.get(url).status_code, 302, url)
        self.assertEqual(self.client.post(reverse("governance:media_rescan")).status_code, 302)
        self.assertEqual(self.client.post(reverse("governance:media_delete_orphan")).status_code, 302)

    def test_admin_manager_and_users_are_denied_everywhere(self):
        message = self.message_with_file()
        self.write_stray("chat_attachments/user_1/stray.txt")
        for user in (self.admin, self.manager, self.owner, self.other):
            self.login(user)
            for url in self._urls(message):
                self.assertEqual(self.client.get(url).status_code, 403, f"{user.role} {url}")
            self.assertEqual(self.client.post(reverse("governance:media_rescan")).status_code, 403)
            resp = self.client.post(
                reverse("governance:media_delete_orphan"),
                {"file": self.stray_token("chat_attachments/user_1/stray.txt")},
            )
            self.assertEqual(resp.status_code, 403)
        self.assertTrue(Path(self.media_root, "chat_attachments/user_1/stray.txt").exists())

    def test_superadmin_sees_the_page_and_the_nav_link_is_superadmin_only(self):
        self.message_with_file(name="visible-report.pdf")
        self.login(self.superadmin)
        response = self.client.get(reverse("governance:media"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "visible-report.pdf")
        self.assertContains(response, "owner@example.com")
        self.assertContains(response, reverse("governance:media"))
        self.login(self.admin)
        self.assertNotContains(self.client.get(reverse("governance:audit_logs")), reverse("governance:media"))


class MediaListingTests(MediaBase):
    """4. pagination, 5. search/filters."""

    def test_pagination_is_server_side(self):
        for i in range(30):
            self.message_with_file(name=f"bulk-{i:02d}.pdf")
        self.login(self.superadmin)
        first = self.client.get(reverse("governance:media"))
        self.assertEqual(first.context["total"], 30)
        self.assertEqual(len(first.context["items"]), media.PAGE_SIZE)
        self.assertEqual(first.context["pages"], 2)
        second = self.client.get(reverse("governance:media"), {"page": 2})
        self.assertEqual(len(second.context["items"]), 5)
        first_ids = {i.pk for i in first.context["items"]}
        self.assertTrue(first_ids.isdisjoint({i.pk for i in second.context["items"]}))
        self.assertEqual(self.client.get(reverse("governance:media"), {"page": "junk"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("governance:media"), {"page": 99999}).status_code, 200)

    def test_search_and_filters(self):
        self.message_with_file(name="budget.pdf", user=self.owner)
        self.message_with_file(name="photo.png", data=PNG, user=self.other)
        self.make_invoice()
        self.login(self.superadmin)
        url = reverse("governance:media")

        def names(**params):
            return sorted(i.display_name for i in self.client.get(url, params).context["items"])

        self.assertEqual(names(q="budget"), ["budget.pdf"])
        self.assertEqual(names(owner="other@"), ["photo.png"])
        self.assertEqual(names(category="image"), ["photo.png", "proof.png"])
        self.assertEqual(names(category="document"), ["budget.pdf"])
        self.assertEqual(names(ext="pdf"), ["budget.pdf"])
        self.assertEqual(names(source="proof"), ["proof.png"])
        self.assertEqual(names(q="Quarterly", source="chat"), ["budget.pdf", "photo.png"])
        self.assertEqual(names(size="large"), [])
        self.assertEqual(names(date_from="2999-01-01"), [])
        self.assertEqual(names(date_from="not-a-date", ext="../etc"), sorted(names()))

    def test_the_media_service_never_writes_to_the_database(self):
        """The branding row is a singleton that SiteBranding.load() would INSERT on first read. (The
        request itself may still create it: the site-wide branding context processor does that.)"""
        SiteBranding.objects.all().delete()
        self.message_with_file()
        media.run_scan()
        media.list_items({})
        media.get_item("branding", 1)
        media.referenced_names()
        self.assertEqual(SiteBranding.objects.count(), 0)

    def test_visibility_is_reported_not_changed(self):
        self.message_with_file()
        self.make_invoice()
        branding = SiteBranding.load()
        branding.logo.save("logo.png", ContentFile(PNG), save=True)
        self.login(self.superadmin)
        by_source = {i.source: i.visibility for i in self.client.get(reverse("governance:media")).context["items"]}
        self.assertEqual(by_source["chat"], "USER-OWNED")
        self.assertEqual(by_source["proof"], "PRIVATE (billing)")
        self.assertEqual(by_source["branding"], "PUBLIC")


class MediaDownloadTests(MediaBase):
    """6. protected download, 7. private stays private, 13. nothing sensitive logged."""

    def test_download_is_an_opaque_attachment_and_is_audited_without_names_or_content(self):
        message = self.message_with_file(name="Salary 2026.html", data=b"<script>x</script>" + SECRET_TEXT)
        # the user-supplied display name is what ends up in headers, so it may hold anything
        Message.objects.filter(pk=message.pk).update(attachment_original_name='Salary <b>2026</b>"\r\nX-Evil: 1.html')
        self.login(self.superadmin)
        url = reverse("governance:media_download", args=["chat", message.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertNotIn("<", response["Content-Disposition"])
        self.assertIn(SECRET_TEXT, b"".join(response.streaming_content))
        self.client.get(url)  # a second click within minutes is not a second record
        rows = AuditLog.objects.filter(action_type="media_download")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.actor, self.superadmin)
        self.assertEqual(row.target_id, f"chat:{message.pk}")
        blob = f"{row.target_type}{row.target_id}{row.old_value}{row.new_value}"
        for forbidden in ("Salary", "html", "SECRET", self.media_root, "chat_attachments"):
            self.assertNotIn(forbidden, blob)

    def test_private_files_stay_private(self):
        message = self.message_with_file(name="contract.pdf")
        invoice = self.make_invoice()
        for name in (message.attachment.name, invoice.submitted_proof_image.name):
            self.assertEqual(self.client.get(f"/media/{name}").status_code, 404)  # anonymous
            self.login(self.other)
            self.assertEqual(self.client.get(f"/media/{name}").status_code, 404)
            self.client.logout()
        self.login(self.superadmin)
        html = self.client.get(reverse("governance:media")).content.decode()
        self.assertNotIn(self.media_root, html)
        self.assertNotIn("chat_attachments/", html)
        self.assertNotIn("invoice_proofs/", html)
        # the owner-facing route still refuses other people after this feature exists
        self.login(self.other)
        owner_url = reverse("chat:download_attachment", args=[message.conversation_id, message.pk])
        self.assertEqual(self.client.get(owner_url).status_code, 404)

    def test_unknown_or_mismatched_items_are_404(self):
        message = self.message_with_file()
        plain = Message.objects.create(conversation=message.conversation, role=Message.Role.USER, content="x")
        self.login(self.superadmin)
        for source, pk in (("nope", message.pk), ("chat", 999999), ("chat", plain.pk), ("generated", message.pk)):
            self.assertEqual(
                self.client.get(reverse("governance:media_download", args=[source, pk])).status_code, 404, (source, pk)
            )
        self.assertEqual(self.client.get("/governance/media/chat/../1/download/").status_code, 404)

    def test_a_referenced_file_that_vanished_is_404_not_a_500(self):
        message = self.message_with_file()
        Path(message.attachment.path).unlink()
        self.login(self.superadmin)
        self.assertEqual(
            self.client.get(reverse("governance:media_download", args=["chat", message.pk])).status_code, 404
        )
        page = self.client.get(reverse("governance:media"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["items"][0].state, "missing")


class MediaPreviewSafetyTests(MediaBase):
    """9. unsafe access rejected, 8 (part). MIME/extension spoofing."""

    def preview(self, message):
        return self.client.get(reverse("governance:media_preview", args=["chat", message.pk]))

    def test_real_images_pdfs_and_text_preview_inline_with_hardened_headers(self):
        self.login(self.superadmin)
        image = self.preview(self.message_with_file(name="ok.png", data=PNG))
        self.assertEqual((image.status_code, image["Content-Type"]), (200, "image/png"))
        self.assertEqual(image["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", image["Content-Security-Policy"])
        pdf = self.preview(self.message_with_file(name="ok.pdf", data=b"%PDF-1.7 body"))
        self.assertEqual((pdf.status_code, pdf["Content-Type"]), (200, "application/pdf"))
        text = self.preview(self.message_with_file(name="notes.txt", data=b"<b>hello</b>"))
        self.assertEqual(text.status_code, 200)
        self.assertTrue(text["Content-Type"].startswith("text/plain"))
        self.assertEqual(text.content, b"<b>hello</b>")

    def test_active_content_and_spoofed_files_are_never_rendered(self):
        self.login(self.superadmin)
        cases = {
            "page.html": b"<html><script>alert(1)</script></html>",
            "vector.svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            "fake.png": b"<html><script>alert(1)</script></html>",  # HTML wearing an image extension
            "fake.pdf": b"<html>not a pdf</html>",
            "binary.txt": b"abc\x00\x01\x02def",
            "tool.exe": b"MZ\x90\x00",
            "archive.zip": b"PK\x03\x04",
        }
        for name, data in cases.items():
            response = self.preview(self.message_with_file(name=name, data=data))
            self.assertEqual(response.status_code, 415, name)
            self.assertEqual(response["Content-Type"], "text/plain", name)

    def test_large_text_previews_are_truncated(self):
        self.login(self.superadmin)
        big = self.message_with_file(name="big.txt", data=b"a" * (media.TEXT_PREVIEW_BYTES * 3))
        body = self.preview(big).content
        self.assertLess(len(body), media.TEXT_PREVIEW_BYTES + 100)
        self.assertIn(b"truncated", body)

    def test_previews_of_private_files_are_audited_once(self):
        message = self.message_with_file(name="ok.png", data=PNG)
        self.login(self.superadmin)
        self.preview(message)
        self.preview(message)
        self.assertEqual(AuditLog.objects.filter(action_type="media_preview").count(), 1)


class MediaPathSafetyTests(MediaBase):
    """8. path traversal rejected."""

    def test_normalise_rejects_anything_that_could_leave_the_media_root(self):
        for bad in ("../x", "a/../../x", "/etc/passwd", "..", ".", "", "a\\..\\b", "a\x00b", "../../etc/passwd"):
            self.assertIsNone(media.normalise_relative_name(bad), repr(bad))
        self.assertEqual(
            media.normalise_relative_name("chat_attachments/user_1/a.pdf"), "chat_attachments/user_1/a.pdf"
        )

    def test_delete_orphan_never_touches_a_file_outside_the_media_root(self):
        outside = Path(self.media_root).parent / "outside-target.txt"
        outside.write_bytes(b"keep")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        for bad in ("../outside-target.txt", "chat_attachments/../../outside-target.txt", str(outside)):
            self.assertEqual(media.delete_orphan(bad), (media.DELETE_INVALID, 0), bad)
        self.assertTrue(outside.exists())

    def test_delete_endpoint_only_accepts_digests_of_scanned_orphans(self):
        outside = Path(self.media_root).parent / "outside-target2.txt"
        outside.write_bytes(b"keep")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        self.login(self.superadmin)
        for token in ("../outside-target2.txt", str(outside), "0" * 16, ""):
            response = self.client.post(reverse("governance:media_delete_orphan"), {"file": token})
            self.assertEqual(response.status_code, 302)
        self.assertTrue(outside.exists())
        self.assertEqual(AuditLog.objects.filter(action_type="media_orphan_deleted").count(), 0)


class MediaOrphanTests(MediaBase):
    """10. referenced file not blindly deletable, 11. orphan detection doesn't delete, 12. audit."""

    ORPHAN = "chat_attachments/user_99/2026/01/leftover.pdf"

    def test_scan_detects_orphans_and_missing_files_without_deleting_anything(self):
        message = self.message_with_file(name="kept.pdf")
        gone = self.message_with_file(name="gone.pdf")
        Path(gone.attachment.path).unlink()
        orphan_path = self.write_stray(self.ORPHAN, b"x" * 100)
        scan = media.run_scan()
        self.assertEqual([o["name"] for o in scan["orphans"]], [self.ORPHAN])
        self.assertEqual(scan["orphan_count"], 1)
        self.assertEqual(scan["orphan_bytes"], 100)
        self.assertEqual(scan["missing"], [gone.attachment.name])
        self.assertTrue(orphan_path.exists())
        self.assertTrue(Path(message.attachment.path).exists())
        self.login(self.superadmin)
        self.assertEqual(self.client.get(reverse("governance:media"), {"view": "orphans"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("governance:media"), {"view": "missing"}).status_code, 200)
        self.assertTrue(orphan_path.exists())

    def test_a_referenced_file_cannot_be_deleted(self):
        message = self.message_with_file(name="kept.pdf")
        name = message.attachment.name
        self.assertEqual(media.delete_orphan(name), (media.DELETE_REFERENCED, 0))
        self.assertTrue(Path(message.attachment.path).exists())
        self.login(self.superadmin)
        token = media.name_digest(name)
        self.client.post(reverse("governance:media_delete_orphan"), {"file": token})
        self.assertTrue(Path(message.attachment.path).exists())

    def test_a_file_that_became_referenced_after_the_scan_is_not_deleted(self):
        path = self.write_stray(self.ORPHAN, b"%PDF-1.4 late")
        self.login(self.superadmin)
        self.client.get(reverse("governance:media"))  # scan cached: file is flagged as an orphan
        conversation = Conversation.objects.create(user=self.owner)
        Message.objects.create(conversation=conversation, role=Message.Role.USER, attachment=self.ORPHAN)
        response = self.client.post(reverse("governance:media_delete_orphan"), {"file": media.name_digest(self.ORPHAN)})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(path.exists())
        blocked = AuditLog.objects.get(action_type="media_delete_blocked")
        self.assertEqual(blocked.new_value, "still referenced")

    def test_payment_proofs_and_branding_count_as_references(self):
        invoice = self.make_invoice()
        branding = SiteBranding.load()
        branding.favicon.save("fav.png", ContentFile(PNG), save=True)
        scan = media.run_scan()
        self.assertEqual(scan["orphan_count"], 0)
        self.assertEqual(media.delete_orphan(invoice.submitted_proof_image.name)[0], media.DELETE_REFERENCED)
        self.assertEqual(media.delete_orphan(branding.favicon.name)[0], media.DELETE_REFERENCED)

    def test_deleting_an_orphan_needs_no_typed_word_and_is_audited_without_the_name(self):
        path = self.write_stray(self.ORPHAN, b"y" * 2048)
        self.login(self.superadmin)
        self.client.get(reverse("governance:media"))
        url = reverse("governance:media_delete_orphan")
        token = media.name_digest(self.ORPHAN)
        response = self.client.post(url, {"file": token})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(path.exists())
        row = AuditLog.objects.get(action_type="media_orphan_deleted")
        self.assertEqual((row.actor, row.target_type, row.target_id), (self.superadmin, "MediaFile", token))
        self.assertEqual(row.old_value, "document 2048B")
        blob = f"{row.target_type}{row.target_id}{row.old_value}{row.new_value}"
        for forbidden in ("leftover", "user_99", "chat_attachments", self.media_root):
            self.assertNotIn(forbidden, blob)
        # gone from the cached scan too, so it cannot be offered again
        self.assertEqual(media.get_scan()["orphan_count"], 0)

    def test_the_delete_dialog_has_no_field_that_shadows_a_window_function(self):
        """Regression (found in a real browser): an <input name="confirm"> inside a form made an inline
        onsubmit="return confirm(...)" call the INPUT, the handler threw, and the form submitted with no
        dialog at all. The dialog is now script-driven; its form may carry only these fields."""
        self.write_stray(self.ORPHAN)
        self.login(self.superadmin)
        html = self.client.get(reverse("governance:media")).content.decode()
        start = html.index('id="mediaDeleteForm"')
        form = html[start : html.index("</form>", start)]
        names = sorted(set(re.findall(r'name="([^"]+)"', form)))
        self.assertEqual(names, ["back", "csrfmiddlewaretoken", "file"])
        self.assertNotIn("onsubmit=", form)
        self.assertNotIn("DELETE", form.replace("Delete", ""))  # nothing to type

    def test_there_is_no_bulk_delete_route(self):
        self.write_stray(self.ORPHAN)
        self.write_stray("chat_attachments/user_99/2026/01/other.pdf")
        self.login(self.superadmin)
        self.client.get(reverse("governance:media"))
        response = self.client.post(
            reverse("governance:media_delete_orphan"),
            {
                "file": [
                    media.name_digest(self.ORPHAN),
                    media.name_digest("chat_attachments/user_99/2026/01/other.pdf"),
                ],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            AuditLog.objects.filter(action_type="media_orphan_deleted").count(), 1
        )  # one file, the last value


class MediaStatisticsTests(MediaBase):
    """14. accurate statistics."""

    def test_totals_categories_and_top_lists_match_what_is_on_disk(self):
        self.message_with_file(name="a.pdf", data=b"%PDF" + b"0" * 96)  # 100 B document
        self.message_with_file(name="b.png", data=PNG)
        self.write_stray("chat_attachments/user_9/blob.bin", b"z" * 1000)  # other + orphan
        scan = media.run_scan()
        self.assertEqual(scan["files"], 3)
        self.assertEqual(scan["bytes"], 100 + len(PNG) + 1000)
        self.assertEqual(scan["by_category"]["document"], {"files": 1, "bytes": 100})
        self.assertEqual(scan["by_category"]["image"], {"files": 1, "bytes": len(PNG)})
        self.assertEqual(scan["by_category"]["other"], {"files": 1, "bytes": 1000})
        self.assertEqual(scan["large"][0]["size"], 1000)
        self.assertEqual(scan["orphan_count"], 1)
        self.assertFalse(scan["partial"])
        self.assertIsNone(scan["error"])

    def test_the_scan_is_cached_not_run_per_request(self):
        self.message_with_file()
        self.login(self.superadmin)
        with mock.patch.object(media, "run_scan", wraps=media.run_scan) as scan:
            self.client.get(reverse("governance:media"))
            self.client.get(reverse("governance:media"), {"q": "x"})
            self.client.get(reverse("governance:media"), {"page": 2})
        self.assertEqual(scan.call_count, 1)

    def test_the_scan_is_bounded(self):
        for i in range(5):
            self.write_stray(f"chat_attachments/user_1/f{i}.bin")
        scan = media.run_scan(max_files=3)
        self.assertEqual(scan["files"], 3)
        self.assertTrue(scan["partial"])
        self.assertEqual(scan["missing_count"], 0)  # a partial walk cannot say what is missing

    def test_rescan_is_rate_limited(self):
        self.login(self.superadmin)
        with mock.patch.object(media, "get_scan", wraps=media.get_scan) as get_scan:
            self.client.post(reverse("governance:media_rescan"))
            self.client.post(reverse("governance:media_rescan"))
        self.assertEqual([c.kwargs.get("force") for c in get_scan.call_args_list], [True])

    def test_disk_health_thresholds(self):
        usage = mock.Mock(total=100, used=85, free=15)
        with mock.patch.object(media.shutil, "disk_usage", return_value=usage):
            self.assertEqual(media.disk_usage()["state"], "WARNING")
        usage = mock.Mock(total=100, used=95, free=5)
        with mock.patch.object(media.shutil, "disk_usage", return_value=usage):
            self.assertEqual(media.disk_usage()["state"], "CRITICAL")
        usage = mock.Mock(total=100, used=10, free=90)
        with mock.patch.object(media.shutil, "disk_usage", return_value=usage):
            self.assertEqual(media.disk_usage()["state"], "NORMAL")


class MediaStorageFailureTests(MediaBase):
    """15. graceful storage failure."""

    def test_a_broken_walk_is_reported_not_raised(self):
        self.message_with_file()
        self.login(self.superadmin)
        with mock.patch.object(media, "_walk_filesystem", side_effect=RuntimeError("disk on fire")):
            response = self.client.get(reverse("governance:media"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be scanned")
        self.assertNotContains(response, "disk on fire")

    def test_a_missing_media_directory_is_an_empty_scan(self):
        shutil.rmtree(self.media_root)
        scan = media.run_scan()
        self.assertEqual((scan["files"], scan["error"]), (0, None))
        self.login(self.superadmin)
        self.assertEqual(self.client.get(reverse("governance:media")).status_code, 200)

    def test_a_file_that_cannot_be_statted_shows_as_unknown(self):
        self.message_with_file()
        self.login(self.superadmin)
        with mock.patch.object(media.default_storage.__class__, "exists", side_effect=OSError("io")):
            response = self.client.get(reverse("governance:media"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["items"][0].state, "unknown")

    def test_an_unreadable_directory_does_not_break_the_page_or_downloads(self):
        message = self.message_with_file()
        self.login(self.superadmin)
        with mock.patch.object(os, "scandir", side_effect=OSError("denied")):
            self.assertEqual(self.client.get(reverse("governance:media")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("governance:media_download", args=["chat", message.pk])).status_code, 200
        )
