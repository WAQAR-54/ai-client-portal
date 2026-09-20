"""Server Media usability pass: the View page, type/reference/size filters, sorting, and the
"Delete file?" flow that replaced typing DELETE.

Security is unchanged and re-proved here where a new path could weaken it: a View never bypasses
authorization, never renders active content, never exposes a path, and the server - not the dialog -
decides whether a file may be deleted."""

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from billing.models import Invoice
from chat.models import Message
from governance import media_service as media
from governance import media_views
from governance.models import AuditLog
from governance.test_media_management import PNG, MediaBase

ORPHAN = "chat_attachments/user_99/2026/01/leftover.pdf"


def names(response):
    return [item.display_name for item in response.context["items"]]


class MediaViewPageTests(MediaBase):
    def view_url(self, message, source="chat"):
        return reverse("governance:media_view", args=[source, message.pk])

    def test_an_image_view_shows_the_image_and_its_dimensions(self):
        message = self.message_with_file(name="chart.png", data=PNG)
        self.login(self.superadmin)
        response = self.client.get(self.view_url(message))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["preview_kind"], "image")
        self.assertEqual(response.context["dimensions"], (1, 1))
        preview = reverse("governance:media_preview", args=["chat", message.pk])
        self.assertContains(response, f'<img class="media-preview-image" src="{preview}"')
        self.assertContains(response, "1 × 1 px")
        self.assertContains(response, reverse("governance:media_download", args=["chat", message.pk]))

    def test_a_pdf_view_embeds_the_hardened_preview_and_offers_download(self):
        message = self.message_with_file(name="report.pdf", data=b"%PDF-1.7 body")
        self.login(self.superadmin)
        response = self.client.get(self.view_url(message))
        self.assertEqual(response.context["preview_kind"], "pdf")
        preview = reverse("governance:media_preview", args=["chat", message.pk])
        self.assertContains(response, f'<iframe class="media-preview-pdf" src="{preview}"')
        self.assertContains(response, "Download")

    def test_a_text_view_shows_escaped_read_only_text(self):
        message = self.message_with_file(name="notes.txt", data=b"hello <script>alert(1)</script> & <b>bold</b>")
        self.login(self.superadmin)
        response = self.client.get(self.view_url(message))
        self.assertEqual(response.context["preview_kind"], "text")
        html = response.content.decode()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<b>bold</b>", html)
        self.assertNotIn("<textarea", html)  # read-only: nothing to edit or submit

    def test_a_long_text_view_is_cut_and_says_so(self):
        message = self.message_with_file(name="big.txt", data=b"a" * (media.TEXT_PREVIEW_BYTES * 3))
        self.login(self.superadmin)
        response = self.client.get(self.view_url(message))
        self.assertTrue(response.context["truncated"])
        self.assertContains(response, "Only the start of the file is shown")
        self.assertLess(len(response.context["text"]), media.TEXT_PREVIEW_BYTES + 10)

    def test_unsupported_and_dangerous_files_show_preview_not_available(self):
        cases = {
            "archive.zip": b"PK\x03\x04",
            "tool.exe": b"MZ\x90\x00",
            "vector.svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            "page.html": b"<html><script>alert(1)</script></html>",
            "fake.png": b"<html><script>alert(2)</script></html>",  # HTML wearing an image extension
            "fake.pdf": b"<html>not a pdf</html>",
            "binary.txt": b"abc\x00\x01\x02",
        }
        self.login(self.superadmin)
        for name, data in cases.items():
            message = self.message_with_file(name=name, data=data)
            response = self.client.get(self.view_url(message))
            self.assertEqual(response.status_code, 200, name)
            self.assertContains(response, "Preview not available", msg_prefix=name)
            self.assertContains(response, reverse("governance:media_download", args=["chat", message.pk]))
            html = response.content.decode()
            for active in ('<img class="media-preview-image"', "<iframe", "<object", "<embed", "<script>alert"):
                self.assertNotIn(active, html, name)

    def test_the_page_states_type_mime_size_owner_related_dates_and_reference(self):
        message = self.message_with_file(name="facts.pdf", data=b"%PDF-1.4 facts")
        self.login(self.superadmin)
        html = self.client.get(self.view_url(message)).content.decode()
        for expected in (
            "facts.pdf",
            "application/pdf",
            "owner@example.com",
            f"Conversation #{message.conversation_id}",
            "Referenced",
            "USER-OWNED",
            "14 bytes",
            "Uploaded",
            "Last modified",
        ):
            self.assertIn(expected, html)

    def test_no_filesystem_path_or_storage_detail_reaches_the_page(self):
        message = self.message_with_file(name="secret-plan.pdf", data=b"%PDF-1.4 x")
        self.login(self.superadmin)
        html = self.client.get(self.view_url(message)).content.decode()
        for leak in (self.media_root, "chat_attachments", "MEDIA_ROOT", "/media/chat_attachments", "/media/branding"):
            self.assertNotIn(leak, html, leak)

    def test_viewing_never_changes_who_can_see_the_file(self):
        message = self.message_with_file(name="private.pdf", data=b"%PDF-1.4 private")
        self.login(self.superadmin)
        self.client.get(self.view_url(message))
        self.client.get(reverse("governance:media_preview", args=["chat", message.pk]))
        self.client.logout()
        self.assertEqual(self.client.get(f"/media/{message.attachment.name}").status_code, 404)
        self.login(self.other)
        self.assertEqual(self.client.get(f"/media/{message.attachment.name}").status_code, 404)
        self.assertEqual(self.client.get(self.view_url(message)).status_code, 403)

    def test_only_a_superadmin_can_view_anything(self):
        message = self.message_with_file()
        self.write_stray(ORPHAN, b"%PDF-1.4 orphan")
        token = media.name_digest(ORPHAN)
        media.get_scan(force=True)
        urls = [
            self.view_url(message),
            reverse("governance:media_view_orphan", args=[token]),
            reverse("governance:media_preview_orphan", args=[token]),
            reverse("governance:media_download_orphan", args=[token]),
        ]
        for url in urls:
            self.client.logout()
            self.assertEqual(self.client.get(url).status_code, 302, url)
            for user in (self.admin, self.manager, self.owner):
                self.login(user)
                self.assertEqual(self.client.get(url).status_code, 403, f"{user.role} {url}")
        self.login(self.superadmin)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_an_unknown_missing_or_mismatched_item_is_404(self):
        message = self.message_with_file()
        self.login(self.superadmin)
        for source, pk in (("nope", message.pk), ("chat", 999999), ("generated", message.pk)):
            self.assertEqual(self.client.get(reverse("governance:media_view", args=[source, pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("governance:media_view_orphan", args=["0" * 16])).status_code, 404)
        self.assertEqual(self.client.get("/governance/media/orphans/../../etc/passwd/").status_code, 404)
        gone = self.message_with_file(name="gone.pdf")
        Message.objects.filter(pk=gone.pk).update()  # the record stays; the file is removed below
        gone.attachment.storage.delete(gone.attachment.name)
        self.assertEqual(self.client.get(self.view_url(gone)).status_code, 404)

    def test_a_storage_failure_is_a_friendly_message_not_a_500(self):
        message = self.message_with_file(name="ok.pdf", data=b"%PDF-1.4 x")
        self.login(self.superadmin)
        with mock.patch.object(media, "read_head", side_effect=OSError("/secret/disk/path exploded")):
            response = self.client.get(self.view_url(message))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be read")
        self.assertNotContains(response, "exploded")
        self.assertNotContains(response, "/secret/disk/path")

    def test_a_text_view_of_a_private_file_is_audited_once_without_content(self):
        message = self.message_with_file(name="n.txt", data=b"TOP-SECRET-CONTENT")
        self.login(self.superadmin)
        self.client.get(self.view_url(message))
        self.client.get(self.view_url(message))
        rows = AuditLog.objects.filter(action_type="media_preview")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertNotIn("SECRET", f"{row.target_id}{row.old_value}{row.new_value}")

    def test_the_back_link_is_rebuilt_from_allowed_parameters_only(self):
        message = self.message_with_file()
        self.login(self.superadmin)
        back = "q=report&category=document&page=2&next=https://evil.example/&redirect=//evil.example"
        response = self.client.get(self.view_url(message), {"back": back})
        url = response.context["back_url"]
        self.assertTrue(url.startswith(reverse("governance:media")))
        self.assertNotIn("evil", url)
        self.assertIn("q=report", url)
        self.assertIn("category=document", url)
        response = self.client.get(self.view_url(message), {"back": "https://evil.example/"})
        self.assertEqual(response.context["back_url"], reverse("governance:media"))

    def test_an_orphan_can_be_viewed_previewed_and_downloaded(self):
        self.write_stray(ORPHAN, b"%PDF-1.4 orphan body")
        token = media.name_digest(ORPHAN)
        self.login(self.superadmin)
        media.get_scan(force=True)
        response = self.client.get(reverse("governance:media_view_orphan", args=[token]))
        self.assertContains(response, "Orphan candidate")
        self.assertEqual(response.context["reference_state"], "orphan")
        preview = self.client.get(reverse("governance:media_preview_orphan", args=[token]))
        self.assertEqual((preview.status_code, preview["Content-Type"]), (200, "application/pdf"))
        download = self.client.get(reverse("governance:media_download_orphan", args=[token]))
        self.assertEqual(download["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", download["Content-Disposition"])

    def test_an_orphan_that_became_referenced_is_shown_as_in_use(self):
        path = self.write_stray(ORPHAN, b"%PDF-1.4 x")
        token = media.name_digest(ORPHAN)
        self.login(self.superadmin)
        media.get_scan(force=True)
        conversation = self.message_with_file().conversation
        Message.objects.create(conversation=conversation, role=Message.Role.USER, attachment=ORPHAN)
        response = self.client.get(reverse("governance:media_view_orphan", args=[token]))
        self.assertEqual(response.context["reference_state"], "referenced")
        self.assertContains(response, "now in use")
        self.assertNotContains(response, "data-media-delete data-token")
        self.assertTrue(path.exists())


class MediaFilterTests(MediaBase):
    def setUp(self):
        super().setUp()
        self.url = reverse("governance:media")
        self.login(self.superadmin)

    def add_mixed(self):
        self.message_with_file(name="photo.png", data=PNG)
        self.message_with_file(name="scan.jpg", data=b"\xff\xd8\xff\xe0jpg")
        self.message_with_file(name="anim.gif", data=b"GIF89a")
        self.message_with_file(name="report.pdf", data=b"%PDF-1.4 x")
        self.message_with_file(name="notes.txt", data=b"text")
        self.message_with_file(name="sheet.docx", data=b"PK\x03\x04docx")
        self.message_with_file(name="vector.svg", data=b"<svg/>")
        self.message_with_file(name="tool.exe", data=b"MZ")
        self.message_with_file(name="legacy.doc", data=b"doc")

    def test_all_images_documents_and_other(self):
        self.add_mixed()
        get = lambda **p: sorted(names(self.client.get(self.url, p)))  # noqa: E731
        self.assertEqual(len(get()), 9)
        self.assertEqual(get(category="image"), ["anim.gif", "photo.png", "scan.jpg"])
        self.assertEqual(get(category="document"), ["notes.txt", "report.pdf", "sheet.docx"])
        self.assertEqual(get(category="other"), ["legacy.doc", "tool.exe", "vector.svg"])

    def test_classification_follows_the_applications_own_upload_rules(self):
        allowed = {e.strip() for e in settings.DEFAULT_ALLOWED_FILE_EXTENSIONS.split(",")}
        for ext in allowed:
            self.assertIn(media.category_of(f"file.{ext}"), ("image", "document"), ext)
        for ext in ("svg", "html", "htm", "exe", "js", "bat", "doc", "xls", "ppt", "rtf", "zip", "mp4", "ico", "bmp"):
            self.assertEqual(media.category_of(f"file.{ext}"), "other", ext)
        self.assertEqual(media.category_of("noextension"), "other")
        self.assertEqual(media.category_of("archive.tar.gz"), "other")
        self.assertEqual(media.category_of("PHOTO.PNG"), "image")

    def test_the_type_chips_keep_the_other_filters_and_mark_the_active_one(self):
        self.add_mixed()
        response = self.client.get(self.url, {"category": "image", "q": "photo", "sort": "oldest"})
        chips = {c["label"]: c for c in response.context["type_chips"]}
        self.assertTrue(chips["Images"]["active"])
        self.assertFalse(chips["All"]["active"])
        self.assertIn("q=photo", chips["Documents"]["url"])
        self.assertIn("sort=oldest", chips["Documents"]["url"])
        self.assertNotIn("category", chips["All"]["url"])
        self.assertNotIn("page=", chips["Other"]["url"])

    def test_filters_work_with_pagination(self):
        for i in range(30):
            self.message_with_file(name=f"img-{i:02d}.png", data=PNG)
        for i in range(5):
            self.message_with_file(name=f"doc-{i}.pdf", data=b"%PDF-1.4 x")
        first = self.client.get(self.url, {"category": "image"})
        self.assertEqual((first.context["total"], len(first.context["items"]), first.context["pages"]), (30, 25, 2))
        self.assertIn("category=image", first.context["next_url"])
        second = self.client.get(self.url, {"category": "image", "page": 2})
        self.assertEqual(len(second.context["items"]), 5)
        self.assertTrue(all(item.category == "image" for item in second.context["items"]))
        self.assertIn("category=image", second.context["prev_url"])
        self.assertEqual(self.client.get(self.url, {"category": "document"}).context["total"], 5)

    def test_search_combines_with_filters_and_clearing_returns_to_all(self):
        self.add_mixed()
        self.message_with_file(name="photo-report.pdf", data=b"%PDF-1.4 y")
        both = self.client.get(self.url, {"q": "report", "category": "document"})
        self.assertEqual(sorted(names(both)), ["photo-report.pdf", "report.pdf"])
        images = self.client.get(self.url, {"q": "photo", "category": "image"})
        self.assertEqual(names(images), ["photo.png"])
        cleared = self.client.get(self.url)
        self.assertEqual(cleared.context["total"], 10)
        self.assertFalse(cleared.context["filters_active"])
        self.assertEqual(cleared.context["clear_url"], self.url)

    def test_invalid_filter_values_are_ignored_not_trusted(self):
        self.add_mixed()
        response = self.client.get(
            self.url, {"category": "'; DROP TABLE x", "sort": "zzz", "size": "nope", "reference": "??", "page": "-4"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 9)
        self.assertEqual(response.context["filters"]["sort"], "newest")

    def test_reference_status_filter_lists_referenced_orphans_or_both(self):
        self.message_with_file(name="kept.pdf", data=b"%PDF-1.4 kept")
        self.write_stray(ORPHAN, b"%PDF-1.4 orphan")
        media.get_scan(force=True)
        both = self.client.get(self.url)
        self.assertEqual(sorted(names(both)), ["kept.pdf", "leftover.pdf"])
        referenced = self.client.get(self.url, {"reference": "referenced"})
        self.assertEqual(names(referenced), ["kept.pdf"])
        orphans = self.client.get(self.url, {"reference": "orphan"})
        self.assertEqual(names(orphans), ["leftover.pdf"])
        self.assertTrue(orphans.context["items"][0].is_orphan)
        legacy = self.client.get(self.url, {"view": "orphans"})  # the old tab address still works
        self.assertEqual(names(legacy), ["leftover.pdf"])

    def test_orphans_obey_the_other_filters_and_have_no_owner(self):
        self.write_stray(ORPHAN, b"%PDF-1.4 orphan")
        self.write_stray("chat_attachments/user_99/2026/01/pic.png", PNG)
        media.get_scan(force=True)
        self.assertEqual(names(self.client.get(self.url, {"category": "image"})), ["pic.png"])
        self.assertEqual(names(self.client.get(self.url, {"q": "leftover"})), ["leftover.pdf"])
        self.assertEqual(names(self.client.get(self.url, {"owner": "someone@example.com"})), [])
        self.assertEqual(names(self.client.get(self.url, {"source": "chat"})), [])

    def test_size_filters_come_from_settings_and_only_match_files_with_a_recorded_size(self):
        self.message_with_file(name="small.pdf", data=b"%PDF" + b"0" * 100)
        self.message_with_file(name="medium.pdf", data=b"%PDF" + b"0" * 5000)
        self.message_with_file(name="large.pdf", data=b"%PDF" + b"0" * 20000)
        self.message_with_file(name="unknown-size.pdf", data=b"%PDF-1.4 x", size=False)
        with override_settings(
            MEDIA_MEDIUM_MIN_BYTES=1000, MEDIA_LARGE_MIN_BYTES=10000, MEDIA_LARGE_THRESHOLDS_MB=[50]
        ):
            get = lambda size: names(self.client.get(self.url, {"size": size}))  # noqa: E731
            self.assertEqual(get("small"), ["small.pdf"])
            self.assertEqual(get("medium"), ["medium.pdf"])
            self.assertEqual(get("large"), ["large.pdf"])
            self.assertEqual([o[0] for o in media.size_options()], ["small", "medium", "large", "ge50"])
            self.assertEqual(get("ge50"), [])

    def test_large_file_thresholds_are_configurable_and_only_above_the_large_limit(self):
        with override_settings(MEDIA_LARGE_MIN_BYTES=10 * 1024**2, MEDIA_LARGE_THRESHOLDS_MB=[5, 50, 100, 500]):
            keys = [o[0] for o in media.size_options()]
        self.assertEqual(keys, ["small", "medium", "large", "ge50", "ge100", "ge500"])  # 5 MB < Large: dropped

    def test_sorting_newest_oldest_largest_smallest_across_sources_and_pages(self):
        old = self.message_with_file(name="old.pdf", data=b"%PDF" + b"0" * 10)
        mid = self.message_with_file(name="mid.pdf", data=b"%PDF" + b"0" * 5000)
        new = self.message_with_file(name="new.pdf", data=b"%PDF" + b"0" * 100)
        now = timezone.now()
        for message, age in ((old, 30), (mid, 20), (new, 10)):
            Message.objects.filter(pk=message.pk).update(created_at=now - timedelta(days=age))
        invoice = self.make_invoice()
        Invoice.objects.filter(pk=invoice.pk).update(submitted_at=now)  # a submitted proof carries its date
        get = lambda sort: names(self.client.get(self.url, {"sort": sort}))  # noqa: E731
        self.assertEqual(get("newest")[:1], ["proof.png"])  # the invoice proof is the newest thing
        self.assertEqual(get("newest")[1:], ["new.pdf", "mid.pdf", "old.pdf"])
        self.assertEqual(get("oldest"), ["old.pdf", "mid.pdf", "new.pdf", "proof.png"])
        self.assertEqual(get("largest"), ["mid.pdf", "new.pdf", "old.pdf", "proof.png"])  # no recorded size: last
        self.assertEqual(get("smallest"), ["old.pdf", "new.pdf", "mid.pdf", "proof.png"])
        self.assertIsNotNone(invoice.pk)

    def test_sorting_is_done_by_the_database_and_pagination_keeps_it(self):
        for i in range(30):
            message = self.message_with_file(name=f"f{i:02d}.pdf", data=b"%PDF" + b"0" * (i + 1))
            self.assertIsNotNone(message.pk)
        first = self.client.get(self.url, {"sort": "largest"})
        second = self.client.get(self.url, {"sort": "largest", "page": 2})
        sizes = [i.size for i in first.context["items"]] + [i.size for i in second.context["items"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertIn("sort=largest", first.context["next_url"])

    def test_the_list_never_walks_the_filesystem_per_request(self):
        self.add_mixed()
        media.get_scan(force=True)
        with mock.patch.object(media, "run_scan", side_effect=AssertionError("walked the storage")):
            for params in ({}, {"category": "image"}, {"q": "photo"}, {"sort": "largest"}, {"reference": "orphan"}):
                self.assertEqual(self.client.get(self.url, params).status_code, 200)


class MediaDeleteFlowTests(MediaBase):
    def setUp(self):
        super().setUp()
        self.login(self.superadmin)
        self.url = reverse("governance:media_delete_orphan")

    def post(self, token, **extra):
        return self.client.post(self.url, {"file": token, **extra}, follow=True)

    def messages(self, response):
        return [str(m) for m in response.context["messages"]]

    def test_the_dialog_is_present_with_cancel_and_a_destructive_delete(self):
        self.write_stray(ORPHAN)
        html = self.client.get(reverse("governance:media")).content.decode()
        self.assertIn("Delete file?", html)
        self.assertIn("This file will be permanently removed.", html)
        self.assertIn('id="mediaDeleteCancel"', html)
        self.assertIn('class="btn btn-danger" id="mediaDeleteSubmit"', html)
        self.assertIn("data-media-delete", html)

    def test_cancel_deletes_nothing_because_nothing_is_sent(self):
        path = self.write_stray(ORPHAN)
        self.client.get(reverse("governance:media"))
        self.assertEqual(self.client.get(self.url).status_code, 405)  # a link or a reload cannot delete
        self.assertTrue(path.exists())

    def test_confirming_deletes_a_safe_file_and_records_the_event(self):
        path = self.write_stray(ORPHAN, b"z" * 10)
        self.client.get(reverse("governance:media"))
        response = self.post(media.name_digest(ORPHAN))
        self.assertIn("File deleted.", self.messages(response))
        self.assertFalse(path.exists())
        self.assertEqual(AuditLog.objects.filter(action_type="media_orphan_deleted").count(), 1)

    def test_the_redirect_returns_to_the_same_filtered_list(self):
        self.write_stray(ORPHAN)
        self.client.get(reverse("governance:media"))
        response = self.client.post(
            self.url, {"file": media.name_digest(ORPHAN), "back": "category=document&sort=largest&evil=1"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("category=document", response.url)
        self.assertIn("sort=largest", response.url)
        self.assertNotIn("evil", response.url)

    def test_a_file_that_became_referenced_after_the_page_loaded_is_not_deleted(self):
        path = self.write_stray(ORPHAN, b"%PDF-1.4 late")
        self.client.get(reverse("governance:media"))  # the page (and the scan) still call it an orphan
        conversation = self.message_with_file().conversation
        Message.objects.create(conversation=conversation, role=Message.Role.USER, attachment=ORPHAN)
        response = self.post(media.name_digest(ORPHAN))
        self.assertIn("This file is now in use and cannot be deleted.", self.messages(response))
        self.assertTrue(path.exists())
        self.assertEqual(AuditLog.objects.get(action_type="media_delete_blocked").new_value, "still referenced")
        self.assertFalse(AuditLog.objects.filter(action_type="media_orphan_deleted").exists())

    def test_the_same_holds_when_the_scan_was_refreshed_after_the_file_became_referenced(self):
        path = self.write_stray(ORPHAN, b"%PDF-1.4 late")
        conversation = self.message_with_file().conversation
        Message.objects.create(conversation=conversation, role=Message.Role.USER, attachment=ORPHAN)
        media.get_scan(force=True)  # the fresh scan no longer lists it
        response = self.post(media.name_digest(ORPHAN))
        self.assertIn("This file is now in use and cannot be deleted.", self.messages(response))
        self.assertTrue(path.exists())

    def test_a_referenced_file_is_never_deletable_by_its_digest(self):
        message = self.message_with_file(name="kept.pdf")
        response = self.post(media.name_digest(message.attachment.name))
        self.assertIn("This file is now in use and cannot be deleted.", self.messages(response))
        self.assertTrue(message.attachment.storage.exists(message.attachment.name))

    def test_an_already_deleted_file_gets_a_clear_message(self):
        path = self.write_stray(ORPHAN)
        self.client.get(reverse("governance:media"))
        path.unlink()  # someone else removed it after the page loaded
        response = self.post(media.name_digest(ORPHAN))
        self.assertIn("This file no longer exists. It may already have been deleted.", self.messages(response))
        response = self.post("f" * 16)  # a digest nobody knows
        self.assertIn("This file no longer exists. It may already have been deleted.", self.messages(response))

    def test_a_storage_failure_is_friendly_and_leaks_nothing(self):
        path = self.write_stray(ORPHAN)
        self.client.get(reverse("governance:media"))
        with mock.patch.object(media.default_storage.__class__, "delete", side_effect=OSError("/var/secret/media EIO")):
            response = self.post(media.name_digest(ORPHAN))
        text = " ".join(self.messages(response))
        self.assertIn("The storage could not remove this file. Nothing was changed.", text)
        for leak in ("/var/secret", "EIO", "Traceback", self.media_root):
            self.assertNotIn(leak, text)
        self.assertTrue(path.exists())
        self.assertFalse(AuditLog.objects.filter(action_type="media_orphan_deleted").exists())

    def test_an_unexpected_error_is_safe_and_carries_a_request_reference(self):
        self.write_stray(ORPHAN)
        self.client.get(reverse("governance:media"))
        with mock.patch.object(media, "orphan_name_for", side_effect=RuntimeError("db exploded: password=hunter2")):
            response = self.post(media.name_digest(ORPHAN))
        text = " ".join(self.messages(response))
        self.assertIn("Something went wrong and the file was not deleted.", text)
        self.assertIn("reference", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("exploded", text)

    def test_permission_is_checked_before_anything_else(self):
        path = self.write_stray(ORPHAN)
        token = media.name_digest(ORPHAN)
        self.client.get(reverse("governance:media"))
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"file": token}).status_code, 302)
        for user in (self.admin, self.manager, self.owner):
            self.login(user)
            self.assertEqual(self.client.post(self.url, {"file": token}).status_code, 403)
        self.assertTrue(path.exists())

    def test_there_is_still_no_bulk_delete_and_no_path_addressing(self):
        first = self.write_stray(ORPHAN)
        second = self.write_stray("chat_attachments/user_99/2026/01/other.pdf")
        self.client.get(reverse("governance:media"))
        for token in ("chat_attachments/user_99/2026/01/other.pdf", "../outside", ORPHAN):
            self.post(token)
        self.assertTrue(first.exists() and second.exists())
        self.client.post(self.url, {"file": [media.name_digest(ORPHAN), media.name_digest(str(second))]})
        remaining = [p for p in (first, second) if p.exists()]
        self.assertGreaterEqual(len(remaining), 1)  # at most one file per request

    def test_a_referenced_row_offers_only_a_disabled_delete(self):
        self.message_with_file(name="kept.pdf")
        html = self.client.get(reverse("governance:media")).content.decode()
        self.assertIn('aria-disabled="true"', html)
        self.assertNotIn("data-media-delete data-token", html)


class MediaUiStateTests(MediaBase):
    def setUp(self):
        super().setUp()
        self.login(self.superadmin)
        self.url = reverse("governance:media")

    def test_empty_states(self):
        self.assertContains(self.client.get(self.url), "No media has been uploaded yet.")
        self.assertContains(
            self.client.get(self.url, {"q": "nothing-matches"}), "No files match your current search or filters."
        )
        self.assertContains(self.client.get(self.url, {"reference": "orphan"}), "No orphan candidates found.")
        self.assertContains(self.client.get(self.url, {"view": "missing"}), "Every referenced file is present.")

    def test_the_filtered_empty_state_offers_a_way_back(self):
        response = self.client.get(self.url, {"category": "image"})
        self.assertContains(response, "Clear filters")

    def test_loading_states_are_wired(self):
        self.message_with_file()
        html = self.client.get(self.url).content.decode()
        self.assertIn("data-media-busy", html)
        self.assertIn("Scanning…", html)
        self.assertIn("Filtering…", html)
        self.assertIn("Deleting…", html)
        self.assertIn("media-admin.js", html)
        self.assertIn('aria-live="polite"', html)

    def test_an_error_state_is_a_banner_not_a_crash(self):
        self.message_with_file()
        with mock.patch.object(media, "_walk_filesystem", side_effect=RuntimeError("boom")):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be scanned")
        self.assertContains(response, 'role="alert"')

    def test_actions_have_accessible_names_not_just_icons(self):
        self.message_with_file(name="named.pdf")
        html = self.client.get(self.url).content.decode()
        for label in ("View named.pdf", "Download named.pdf", "Delete named.pdf"):
            self.assertIn(label, html)

    def test_the_summary_shows_real_numbers_including_orphans(self):
        self.message_with_file(name="a.pdf", data=b"%PDF" + b"0" * 96)
        self.write_stray(ORPHAN, b"x" * 50)
        response = self.client.get(self.url)
        scan = response.context["scan"]
        self.assertEqual((scan["files"], scan["orphan_count"], scan["orphan_bytes"]), (2, 1, 50))
        self.assertContains(response, "Orphan candidates")

    def test_the_table_uses_the_requested_columns(self):
        self.message_with_file()
        html = self.client.get(self.url).content.decode()
        head = html[html.index("<thead>") : html.index("</thead>")]
        order = [head.index(c) for c in ("Preview", "File", "Type", "Size", "Owner", "Status", "Uploaded", "Actions")]
        self.assertEqual(order, sorted(order))


class MediaSecurityRegressionTests(MediaBase):
    def test_traversal_and_spoofing_protections_are_unchanged(self):
        self.login(self.superadmin)
        for bad in ("../x", "a/../../x", "/etc/passwd", "a\\..\\b", "a\x00b"):
            self.assertIsNone(media.normalise_relative_name(bad), repr(bad))
        message = self.message_with_file(name="fake.png", data=b"<html><script>alert(1)</script></html>")
        self.assertEqual(
            self.client.get(reverse("governance:media_preview", args=["chat", message.pk])).status_code, 415
        )
        download = self.client.get(reverse("governance:media_download", args=["chat", message.pk]))
        self.assertEqual(download["Content-Type"], "application/octet-stream")
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")

    def test_the_detail_page_never_links_to_a_public_media_url(self):
        message = self.message_with_file(name="p.png", data=PNG)
        self.login(self.superadmin)
        html = self.client.get(reverse("governance:media_view", args=["chat", message.pk])).content.decode()
        self.assertNotIn("/media/chat_attachments", html)
        self.assertNotIn(message.attachment.name, html)
        self.assertIsNotNone(media_views)  # the views module is what serves the preview URLs above

    def test_the_content_is_read_through_the_storage_not_through_a_path(self):
        message = self.message_with_file(name="s.pdf", data=b"%PDF-1.4 x")
        self.login(self.superadmin)
        with mock.patch("governance.media_views.default_storage.open", side_effect=OSError("/p/a/t/h")):
            response = self.client.get(reverse("governance:media_download", args=["chat", message.pk]))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"/p/a/t/h", response.content)
        self.assertIsNotNone(ContentFile)
