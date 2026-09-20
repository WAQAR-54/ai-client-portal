"""Server Media bulk delete: the browser's selection is a suggestion, the server decides file by file.

Every id is re-resolved, every file re-checked (still there, no record refers to it) at the moment of deletion,
partial success is reported exactly, and nothing a browser sends can name a path or exceed the batch limit."""

import json
from pathlib import Path
from unittest import mock

from django.test import Client
from django.urls import reverse

from chat.models import Message
from governance import media_service as media
from governance.models import AuditLog
from governance.test_media_management import PNG, MediaBase

BASE = "chat_attachments/user_99/2026/01"


def digest_id(relative):
    return f"orphan:{media.name_digest(relative)}"


class BulkBase(MediaBase):
    def setUp(self):
        super().setUp()
        self.login(self.superadmin)
        self.url = reverse("governance:media_bulk_delete")
        self.counter = 0

    def orphan(self, name, data=b"x" * 100):
        relative = f"{BASE}/{name}"
        return relative, self.write_stray(relative, data)

    def scan(self):
        media.get_scan(force=True)

    def bulk(self, ids, op="op-12345678", chunk=0, client=None):
        response = (client or self.client).post(self.url, {"ids": ids, "op": op, "chunk": chunk})
        return response, (json.loads(response.content) if response["Content-Type"] == "application/json" else None)

    def fresh_op(self):
        self.counter += 1
        return f"op-{self.counter:08d}"


class BulkAccessTests(BulkBase):
    def test_anonymous_is_sent_to_login_and_nothing_is_deleted(self):
        relative, path = self.orphan("a.pdf")
        self.scan()
        self.client.logout()
        self.assertEqual(
            self.client.post(self.url, {"ids": [digest_id(relative)], "op": "op-12345678"}).status_code, 302
        )
        self.assertEqual(self.client.get(reverse("governance:media_bulk_selectable")).status_code, 302)
        self.assertTrue(path.exists())

    def test_admin_manager_and_users_are_refused(self):
        relative, path = self.orphan("a.pdf")
        self.scan()
        for user in (self.admin, self.manager, self.owner, self.other):
            self.login(user)
            self.assertEqual(self.bulk([digest_id(relative)], op=self.fresh_op())[0].status_code, 403, user.role)
            self.assertEqual(self.client.get(reverse("governance:media_bulk_selectable")).status_code, 403, user.role)
        self.assertTrue(path.exists())
        self.assertFalse(AuditLog.objects.filter(action_type="media_bulk_delete").exists())

    def test_a_get_cannot_delete_anything(self):
        relative, path = self.orphan("a.pdf")
        self.scan()
        self.assertEqual(self.client.get(self.url, {"ids": digest_id(relative)}).status_code, 405)
        self.assertTrue(path.exists())

    def test_csrf_is_enforced(self):
        relative, path = self.orphan("a.pdf")
        self.scan()
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.superadmin)
        self.assertEqual(strict.post(self.url, {"ids": [digest_id(relative)], "op": "op-12345678"}).status_code, 403)
        self.assertTrue(path.exists())


class BulkDeleteBehaviourTests(BulkBase):
    def test_safe_files_are_deleted_and_the_space_recovered_is_real(self):
        first, p1 = self.orphan("one.pdf", b"a" * 1000)
        second, p2 = self.orphan("two.txt", b"b" * 234)
        self.scan()
        response, data = self.bulk([digest_id(first), digest_id(second)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            data,
            {
                "ok": True,
                "selected": 2,
                "deleted": 2,
                "skipped_referenced": 0,
                "skipped_missing": 0,
                "failed": 0,
                "freed_bytes": 1234,
            },
        )
        self.assertFalse(p1.exists() or p2.exists())
        self.assertEqual(media.get_scan()["orphan_count"], 0)  # the scan cache was refreshed

    def test_referenced_files_are_skipped_never_deleted(self):
        message = self.message_with_file(name="kept.pdf")
        invoice = self.make_invoice()
        orphan, path = self.orphan("free.pdf")
        self.scan()
        ids = [f"chat:{message.pk}", f"proof:{invoice.pk}", digest_id(message.attachment.name), digest_id(orphan)]
        _response, data = self.bulk(ids)
        self.assertEqual((data["deleted"], data["skipped_referenced"]), (1, 3))
        self.assertTrue(Path(message.attachment.path).exists())
        self.assertTrue(Path(invoice.submitted_proof_image.path).exists())
        self.assertFalse(path.exists())

    def test_a_file_that_became_referenced_after_the_scan_is_skipped(self):
        orphan, path = self.orphan("late.pdf", b"%PDF-1.4 late")
        other, other_path = self.orphan("still-free.pdf")
        self.scan()  # both are orphan candidates on the page
        conversation = self.message_with_file().conversation
        Message.objects.create(conversation=conversation, role=Message.Role.USER, attachment=orphan)
        _response, data = self.bulk([digest_id(orphan), digest_id(other)])
        self.assertEqual((data["deleted"], data["skipped_referenced"]), (1, 1))
        self.assertTrue(path.exists())
        self.assertFalse(other_path.exists())

    def test_a_file_that_vanished_or_never_existed_is_skipped_as_missing(self):
        gone, gone_path = self.orphan("gone.pdf")
        kept, kept_path = self.orphan("kept.pdf")
        self.scan()
        gone_path.unlink()
        _response, data = self.bulk([digest_id(gone), digest_id(kept), "orphan:" + "f" * 16])
        self.assertEqual((data["deleted"], data["skipped_missing"], data["failed"]), (1, 2, 0))
        self.assertFalse(kept_path.exists())

    def test_a_record_id_whose_record_is_gone_counts_as_missing(self):
        _response, data = self.bulk(["chat:999999", "proof:999999", "branding:1"])
        self.assertEqual((data["skipped_missing"], data["skipped_referenced"], data["deleted"]), (3, 0, 0))

    def test_a_storage_failure_on_one_file_is_reported_and_the_rest_still_go(self):
        a, pa = self.orphan("a.pdf")
        b, pb = self.orphan("b.pdf")
        c, pc = self.orphan("c.pdf")
        self.scan()
        storage_class = media.default_storage.__class__
        real_delete = storage_class.delete

        def flaky(storage, name):
            if name.endswith("b.pdf"):
                raise OSError("/var/secret/path EIO")
            return real_delete(storage, name)

        with mock.patch.object(storage_class, "delete", flaky):
            response, data = self.bulk([digest_id(a), digest_id(b), digest_id(c)])
        self.assertEqual((data["deleted"], data["failed"]), (2, 1))
        self.assertNotIn("EIO", response.content.decode())
        self.assertTrue(pb.exists())
        self.assertFalse(pa.exists() or pc.exists())

    def test_partial_success_is_reported_exactly(self):
        ok1, _ = self.orphan("ok1.pdf")
        ok2, _ = self.orphan("ok2.pdf")
        gone, gone_path = self.orphan("gone.pdf")
        bad = self.message_with_file(name="ref.pdf")
        self.scan()
        gone_path.unlink()
        _response, data = self.bulk(
            [digest_id(ok1), digest_id(ok2), digest_id(gone), f"chat:{bad.pk}", "not-an-id"], op=self.fresh_op()
        )
        self.assertEqual(
            (data["selected"], data["deleted"], data["skipped_referenced"], data["skipped_missing"], data["failed"]),
            (5, 2, 1, 1, 1),
        )
        self.assertEqual(data["deleted"] + data["skipped_referenced"] + data["skipped_missing"] + data["failed"], 5)

    def test_duplicate_ids_in_one_request_are_counted_once(self):
        a, _ = self.orphan("a.pdf")
        self.scan()
        _response, data = self.bulk([digest_id(a)] * 4)
        self.assertEqual((data["selected"], data["deleted"]), (1, 1))

    def test_no_filesystem_path_is_ever_accepted(self):
        outside = Path(self.media_root).parent / "outside-bulk.txt"
        outside.write_bytes(b"keep")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        orphan, path = self.orphan("a.pdf")
        self.scan()
        hostile = [
            orphan,  # the storage-relative path itself
            "../outside-bulk.txt",
            str(outside),
            "orphan:../outside-bulk.txt",
            "orphan:" + orphan,
            f"chat:1;{orphan}",
            "chat:-1",
            "orphan:" + "G" * 16,
            "",
            "x" * 5000,
        ]
        response, data = self.bulk(hostile, op=self.fresh_op())
        self.assertEqual(response.status_code, 200)
        self.assertEqual((data["deleted"], data["failed"]), (0, len(set(hostile))))
        self.assertTrue(path.exists())
        self.assertTrue(outside.exists())

    def test_the_batch_limit_is_enforced_before_anything_is_touched(self):
        orphan, path = self.orphan("a.pdf")
        self.scan()
        too_many = [digest_id(orphan)] + [f"chat:{n}" for n in range(media.BULK_MAX_PER_REQUEST)]
        response, data = self.bulk(too_many, op=self.fresh_op())
        self.assertEqual((response.status_code, data["ok"]), (400, False))
        self.assertTrue(path.exists())
        self.assertFalse(AuditLog.objects.filter(action_type="media_bulk_delete").exists())
        exactly = [digest_id(orphan)] + [f"chat:{n}" for n in range(media.BULK_MAX_PER_REQUEST - 1)]
        self.assertEqual(self.bulk(exactly, op=self.fresh_op())[0].status_code, 200)

    def test_an_empty_or_malformed_request_is_refused(self):
        self.assertEqual(self.bulk([], op=self.fresh_op())[0].status_code, 400)
        self.assertEqual(self.bulk(["chat:1"], op="")[0].status_code, 400)
        self.assertEqual(self.bulk(["chat:1"], op="../../etc")[0].status_code, 400)

    def test_submitting_the_same_step_twice_deletes_and_audits_once(self):
        a, _ = self.orphan("a.pdf")
        self.scan()
        op = self.fresh_op()
        first, _data = self.bulk([digest_id(a)], op=op, chunk=0)
        second, again = self.bulk([digest_id(a)], op=op, chunk=0)  # a double click
        self.assertEqual((first.status_code, second.status_code), (200, 409))
        self.assertIn("already submitted", again["error"])
        self.assertEqual(AuditLog.objects.filter(action_type="media_bulk_delete").count(), 1)
        third, _ = self.bulk(["chat:1"], op=op, chunk=1)  # the next step of the same operation is allowed
        self.assertEqual(third.status_code, 200)

    def test_an_unexpected_error_is_a_friendly_json_error_without_details(self):
        with mock.patch.object(media, "bulk_delete", side_effect=RuntimeError("db exploded password=hunter2")):
            response, data = self.bulk(["chat:1"], op=self.fresh_op())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("hunter2", response.content.decode())
        self.assertNotIn("exploded", response.content.decode())
        self.assertIn("not completed", data["error"])

    def test_one_audit_row_per_request_with_counts_and_nothing_sensitive(self):
        a, _ = self.orphan("secret-name-a.pdf")
        b, _ = self.orphan("secret-name-b.pdf", b"z" * 50)
        ref = self.message_with_file(name="ref.pdf")
        self.scan()
        self.bulk([digest_id(a), digest_id(b), f"chat:{ref.pk}"], op="op-audit0001", chunk=3)
        row = AuditLog.objects.get(action_type="media_bulk_delete")
        self.assertEqual(
            (row.actor, row.target_type, row.target_id), (self.superadmin, "MediaFile", "bulk:op-audit0001")
        )
        self.assertEqual(row.old_value, "selected=3 deleted=2 skipped_referenced=1 skipped_missing=0 failed=0")
        self.assertEqual(row.new_value, "freed=150B step=3")
        blob = f"{row.target_id}{row.old_value}{row.new_value}"
        for forbidden in ("secret-name", "user_99", "chat_attachments", self.media_root):
            self.assertNotIn(forbidden, blob)

    def test_orphans_are_never_deleted_automatically_only_by_deliberate_selection(self):
        a, path = self.orphan("a.pdf")
        self.scan()
        self.client.get(reverse("governance:media"))
        self.client.get(reverse("governance:media_bulk_selectable"))  # asking what could be selected deletes nothing
        self.assertTrue(path.exists())
        self.assertFalse(AuditLog.objects.filter(action_type="media_bulk_delete").exists())


class BulkSelectionTests(BulkBase):
    def selectable(self, **params):
        response = self.client.get(reverse("governance:media_bulk_selectable"), params)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def test_select_all_matching_follows_the_current_filters(self):
        self.orphan("pic1.png", PNG)
        self.orphan("pic2.png", PNG)
        self.orphan("doc.pdf", b"%PDF-1.4 x")
        self.message_with_file(name="photo.png", data=PNG)
        self.message_with_file(name="report.pdf", data=b"%PDF-1.4 y")
        self.scan()
        everything = self.selectable()
        self.assertEqual((everything["total"], everything["eligible"], everything["referenced"]), (5, 3, 2))
        images = self.selectable(category="image")
        self.assertEqual((images["total"], images["eligible"], images["referenced"]), (3, 2, 1))
        self.assertEqual(sorted(images["ids"]), sorted([digest_id(f"{BASE}/pic1.png"), digest_id(f"{BASE}/pic2.png")]))
        searched = self.selectable(q="doc")
        self.assertEqual(
            (searched["total"], searched["eligible"], searched["ids"]), (1, 1, [digest_id(f"{BASE}/doc.pdf")])
        )
        only_referenced = self.selectable(reference="referenced")
        self.assertEqual((only_referenced["eligible"], only_referenced["ids"]), (0, []))
        none = self.selectable(q="zzz-no-match")
        self.assertEqual((none["total"], none["eligible"]), (0, 0))

    def test_filtered_selection_never_includes_files_outside_the_filter(self):
        self.orphan("pic.png", PNG)
        doc, doc_path = self.orphan("doc.pdf", b"%PDF-1.4 x")
        self.scan()
        chosen = self.selectable(category="image")["ids"]
        self.assertNotIn(digest_id(doc), chosen)
        self.bulk(chosen)
        self.assertTrue(doc_path.exists())

    def test_the_selection_is_capped_and_says_so(self):
        for i in range(6):
            self.orphan(f"f{i}.pdf")
        self.scan()
        with mock.patch.object(media, "BULK_MAX_PER_OPERATION", 4):
            data = self.selectable()
        self.assertEqual((data["eligible"], len(data["ids"]), data["truncated"], data["limit"]), (6, 4, True, 4))

    def test_the_selectable_answer_is_ids_only_never_a_name_or_path(self):
        relative, _ = self.orphan("private-name.pdf")
        self.scan()
        raw = self.client.get(reverse("governance:media_bulk_selectable")).content.decode()
        for leak in ("private-name", "user_99", "chat_attachments", self.media_root):
            self.assertNotIn(leak, raw)
        self.assertIn(digest_id(relative), raw)


class BulkPageTests(BulkBase):
    def test_every_row_carries_a_checkbox_with_an_opaque_id_and_an_eligibility_flag(self):
        message = self.message_with_file(name="kept.pdf")
        relative, _ = self.orphan("free.pdf")
        self.scan()
        response = self.client.get(reverse("governance:media"))
        html = response.content.decode()
        self.assertIn(f'value="chat:{message.pk}" data-eligible="0"', html)
        self.assertIn(f'value="{digest_id(relative)}" data-eligible="1"', html)
        self.assertIn('id="mediaSelectPage"', html)  # select current page
        self.assertIn('aria-label="Select all files on this page"', html)
        self.assertIn("Select kept.pdf", html)
        for leak in (self.media_root, "chat_attachments/"):
            self.assertNotIn(leak, html)

    def test_the_selection_bar_shows_a_count_and_offers_select_all_matching(self):
        for i in range(3):
            self.message_with_file(name=f"f{i}.pdf")
        html = self.client.get(reverse("governance:media"), {"category": "document"}).content.decode()
        self.assertIn('id="mediaBulkBar"', html)
        self.assertIn('data-t-count="Selected: {n}"', html)
        self.assertIn('id="mediaSelectAllFiltered"', html)
        self.assertIn("Select all 3 matching files", html)
        self.assertIn("category=document", html)  # the filter travels with the selectable-url
        self.assertIn("Delete selected", html)

    def test_the_confirmation_dialog_is_a_normal_dialog_with_cancel_and_a_danger_button(self):
        self.message_with_file()
        html = self.client.get(reverse("governance:media")).content.decode()
        start = html.index('id="mediaBulkModal"')
        dialog = html[start : html.index("</form>", start)]
        for expected in (
            "Delete selected files?",
            'data-t-referenced="{n} are in use by a record and will be skipped."',
            'data-t-eligible="{n} can be deleted now."',
            "Cancel",
            'class="btn btn-danger" id="mediaBulkConfirmButton"',
            "Deleted files cannot be restored",
        ):
            self.assertIn(expected, dialog)
        self.assertNotIn('type="text"', dialog)  # nothing to type
        self.assertNotIn("DELETE ", dialog.replace("Delete", ""))

    def test_the_bulk_controls_are_hidden_until_the_script_runs_and_absent_from_the_missing_tab(self):
        self.message_with_file()
        html = self.client.get(reverse("governance:media")).content.decode()
        self.assertIn('id="mediaBulkBar" hidden', html)
        self.assertIn('class="media-select" hidden', html)
        missing = self.client.get(reverse("governance:media"), {"view": "missing"}).content.decode()
        self.assertNotIn("mediaBulkBar", missing)

    def test_an_empty_list_has_no_bulk_controls(self):
        html = self.client.get(reverse("governance:media")).content.decode()
        self.assertNotIn("mediaBulkBar", html)
        self.assertNotIn("mediaBulkModal", html)
