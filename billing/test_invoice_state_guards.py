"""Invoice status changes are decided against the CURRENT status, under a row lock.

Regression for: verify/reject/toggle read no state at all, so a second admin, a double click or
a stale page could re-verify a rejected invoice, write duplicate audit entries, or flip a
REFUNDED invoice back to PAID and re-assign the plan for money that had been returned.
(select_for_update is a no-op on SQLite; these tests pin the state-machine behaviour, which is
what the lock protects on Postgres.)"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, User
from billing.models import Invoice
from governance.models import AuditLog, Plan
from governance.plans import get_assignment


class InvoiceStateGuardTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Dept A")
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.department
        )
        self.recipient = User.objects.create_user(
            email="r@example.com", password="pw12345!", department=self.department
        )
        self.plan = Plan.objects.get(name="Premium")
        self.client.force_login(self.admin)

    def _invoice(self, status):
        return Invoice.objects.create(
            department=self.department,
            recipient_user=self.recipient,
            plan=self.plan,
            issue_date=timezone.localdate() - timedelta(days=3),
            due_date=timezone.localdate() + timedelta(days=11),
            currency="USD",
            subtotal=Decimal("50"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("50"),
            status=status,
        )

    def _post(self, name, invoice):
        return self.client.post(reverse(f"billing:{name}", args=[invoice.id]))

    def _audits(self, action):
        return AuditLog.objects.filter(action_type=action).count()

    def _plan_of_recipient(self):
        assignment = get_assignment(self.recipient)
        return assignment.plan if assignment else None

    def test_a_waiting_proof_can_be_verified(self):
        invoice = self._invoice(Invoice.Status.PENDING_VERIFICATION)
        self._post("verify_invoice_payment", invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        self.assertEqual(invoice.verified_by, self.admin)
        self.assertEqual(self._plan_of_recipient(), self.plan)

    def test_verifying_twice_writes_one_audit_entry_and_keeps_the_first_reviewer(self):
        invoice = self._invoice(Invoice.Status.PENDING_VERIFICATION)
        self._post("verify_invoice_payment", invoice)
        first = Invoice.objects.get(pk=invoice.pk).verified_at
        other = User.objects.create_user(
            email="admin2@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.department
        )
        self.client.force_login(other)
        self._post("verify_invoice_payment", invoice)
        invoice.refresh_from_db()
        self.assertEqual(self._audits("billing.invoice_payment_verified"), 1)
        self.assertEqual(invoice.verified_by, self.admin)
        self.assertEqual(invoice.verified_at, first)

    def test_a_refunded_invoice_cannot_be_verified_back_to_paid_or_hand_the_plan_back(self):
        invoice = self._invoice(Invoice.Status.REFUNDED)
        self._post("verify_invoice_payment", invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.REFUNDED)
        self.assertEqual(self._audits("billing.invoice_payment_verified"), 0)
        self.assertNotEqual(self._plan_of_recipient(), self.plan)

    def test_a_paid_invoice_cannot_be_rejected_back_to_unpaid(self):
        invoice = self._invoice(Invoice.Status.PAID)
        self._post("reject_invoice_payment", invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        self.assertEqual(self._audits("billing.invoice_payment_rejected"), 0)

    def test_an_unpaid_invoice_has_nothing_to_verify_or_reject(self):
        invoice = self._invoice(Invoice.Status.UNPAID)
        self._post("verify_invoice_payment", invoice)
        self._post("reject_invoice_payment", invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.UNPAID)
        self.assertEqual(AuditLog.objects.filter(action_type__startswith="billing.invoice_payment").count(), 0)

    def test_rejecting_a_waiting_proof_returns_it_to_unpaid_once(self):
        invoice = self._invoice(Invoice.Status.PENDING_VERIFICATION)
        self._post("reject_invoice_payment", invoice)
        self._post("reject_invoice_payment", invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.UNPAID)
        self.assertEqual(self._audits("billing.invoice_payment_rejected"), 1)

    def test_the_manual_toggle_still_flips_unpaid_and_paid(self):
        invoice = self._invoice(Invoice.Status.UNPAID)
        self._post("toggle_invoice_status", invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        self._post("toggle_invoice_status", invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.UNPAID)
        self.assertEqual(self._audits("billing.invoice_status_toggle"), 2)

    def test_the_manual_toggle_refuses_a_refunded_invoice(self):
        invoice = self._invoice(Invoice.Status.REFUNDED)
        self._post("toggle_invoice_status", invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.REFUNDED)
        self.assertEqual(self._audits("billing.invoice_status_toggle"), 0)
        self.assertNotEqual(self._plan_of_recipient(), self.plan)

    def test_a_department_admin_still_cannot_touch_another_departments_invoice(self):
        invoice = self._invoice(Invoice.Status.PENDING_VERIFICATION)
        invoice.department = Department.objects.create(name="Dept B")
        invoice.save()
        self.assertEqual(self._post("verify_invoice_payment", invoice).status_code, 403)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PENDING_VERIFICATION)
