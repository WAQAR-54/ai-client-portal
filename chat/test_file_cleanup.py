"""Uploaded files are removed together with the rows that own them.

Regression for: the retention sweep (Conversation.delete(), cascading to Messages) removed the
database rows but left every attachment on disk permanently."""

import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.core.files.base import ContentFile
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Department, User
from billing.models import Invoice
from chat.models import Conversation, Message
from governance.models import Plan
from governance.tasks import sweep_conversation_retention


class FileCleanupTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(email="files@example.com", password="pw12345!")
        self.conversation = Conversation.objects.create(user=self.user)

    def _message_with_file(self, conversation=None, name="contract.pdf"):
        message = Message.objects.create(
            conversation=conversation or self.conversation, role=Message.Role.USER, content="see file"
        )
        message.attachment.save(name, ContentFile(b"%PDF private"), save=True)
        return message

    def _exists(self, field_file):
        return Path(field_file.path).exists()

    def test_deleting_a_message_removes_its_file_after_the_commit(self):
        message = self._message_with_file()
        path = Path(message.attachment.path)
        self.assertTrue(path.exists())
        with self.captureOnCommitCallbacks(execute=True):
            message.delete()
        self.assertFalse(path.exists())

    def test_deleting_a_conversation_removes_every_attachment_in_it(self):
        paths = [Path(self._message_with_file(name=f"f{i}.pdf").attachment.path) for i in range(3)]
        with self.captureOnCommitCallbacks(execute=True):
            self.conversation.delete()
        self.assertEqual([p.exists() for p in paths], [False, False, False])

    def test_a_rolled_back_delete_keeps_the_file(self):
        message = self._message_with_file()
        path, pk = Path(message.attachment.path), message.pk  # delete() clears message.pk
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    message.delete()
                    raise RuntimeError("something later in the request failed")
            except RuntimeError:
                pass
        self.assertTrue(path.exists())
        self.assertTrue(Message.objects.filter(pk=pk).exists())

    def test_a_message_without_a_file_or_with_an_already_missing_file_deletes_cleanly(self):
        plain = Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="hi")
        gone = self._message_with_file(name="gone.pdf")
        Path(gone.attachment.path).unlink()
        with self.captureOnCommitCallbacks(execute=True):
            plain.delete()
            gone.delete()
        self.assertFalse(Message.objects.filter(pk__in=[plain.pk, gone.pk]).exists())

    def test_another_users_file_is_never_touched(self):
        other_user = User.objects.create_user(email="other-files@example.com", password="pw12345!")
        keep = self._message_with_file(Conversation.objects.create(user=other_user), name="keep.pdf")
        with self.captureOnCommitCallbacks(execute=True):
            self.conversation.delete()
        self.assertTrue(self._exists(keep.attachment))

    def test_the_retention_sweep_now_removes_the_files_it_orphaned_before(self):
        department = Department.objects.create(name="Retention", retention_period=Department.RetentionPeriod.DAYS_30)
        User.objects.filter(pk=self.user.pk).update(department=department)
        message = self._message_with_file()
        path = Path(message.attachment.path)
        Conversation.objects.filter(pk=self.conversation.pk).update(updated_at=timezone.now() - timedelta(days=90))
        with self.captureOnCommitCallbacks(execute=True):
            sweep_conversation_retention()
        self.assertFalse(Conversation.all_objects.filter(pk=self.conversation.pk).exists())
        self.assertFalse(path.exists())

    def test_deleting_an_invoice_removes_its_payment_proof(self):
        invoice = Invoice.objects.create(
            department=None,
            recipient_user=self.user,
            plan=Plan.objects.get(name="Premium"),
            issue_date=timezone.localdate(),
            due_date=timezone.localdate() + timedelta(days=14),
            currency="USD",
            subtotal=Decimal("50"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("50"),
        )
        invoice.submitted_proof_image.save("proof.png", ContentFile(b"png"), save=True)
        path = Path(invoice.submitted_proof_image.path)
        self.assertTrue(path.exists())
        with self.captureOnCommitCallbacks(execute=True):
            invoice.delete()
        self.assertFalse(path.exists())
