"""The daily new-conversation limit is enforced atomically and cannot be walked past.

Two real defects fixed here: (1) check-then-create was not race-protected, so simultaneous
requests from one user all passed the check; (2) conversations the user had soft-deleted no longer
counted, so start-delete-start-delete never reached the limit."""

import threading
from unittest import mock
from unittest import skipUnless

from django.db import connection, connections
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import User
from chat.models import Conversation
from governance.limits import UsageLimitExceeded
from governance.models import Plan
from governance.plans import assign_plan, create_conversation_within_quota


def _user_on_limited_plan(email, limit):
    plan = Plan.objects.get(name="Premium")
    Plan.objects.filter(pk=plan.pk).update(sessions_per_day_limit=limit)
    user = User.objects.create_user(email=email, password="pw12345!")
    assign_plan(user, Plan.objects.get(pk=plan.pk))
    return user


class ConversationQuotaTests(TestCase):
    def setUp(self):
        self.user = _user_on_limited_plan("quota@example.com", 2)

    def test_creates_up_to_the_limit_then_refuses_without_creating(self):
        create_conversation_within_quota(self.user, title="one")
        create_conversation_within_quota(self.user, title="two")
        with self.assertRaises(UsageLimitExceeded):
            create_conversation_within_quota(self.user, title="three")
        self.assertEqual(Conversation.all_objects.filter(user=self.user).count(), 2)

    def test_deleting_a_conversation_does_not_give_the_quota_back(self):
        first = create_conversation_within_quota(self.user, title="one")
        second = create_conversation_within_quota(self.user, title="two")
        Conversation.objects.filter(pk__in=[first.pk, second.pk]).update(is_deleted=True)
        self.assertEqual(Conversation.objects.filter(user=self.user).count(), 0)
        with self.assertRaises(UsageLimitExceeded):
            create_conversation_within_quota(self.user, title="three")

    def test_the_limit_is_per_user(self):
        other = _user_on_limited_plan("other-quota@example.com", 2)
        create_conversation_within_quota(self.user)
        create_conversation_within_quota(self.user)
        self.assertIsNotNone(create_conversation_within_quota(other))

    def test_a_plan_without_a_limit_is_unrestricted(self):
        unlimited = _user_on_limited_plan("unlimited@example.com", None)
        for _ in range(5):
            create_conversation_within_quota(unlimited)
        self.assertEqual(Conversation.all_objects.filter(user=unlimited).count(), 5)

    def test_the_users_row_is_locked_before_the_count(self):
        """Ordering matters: the lock must be taken BEFORE the count, or the re-check protects nothing."""
        order = []
        real_lock = QuerySet.select_for_update
        real_count = QuerySet.count

        def spy_lock(qs, *args, **kwargs):
            order.append("lock")
            return real_lock(qs, *args, **kwargs)

        def spy_count(qs):
            if qs.model is Conversation:
                order.append("count")
            return real_count(qs)

        with mock.patch.object(QuerySet, "select_for_update", spy_lock), mock.patch.object(
            QuerySet, "count", spy_count
        ):
            create_conversation_within_quota(self.user)
        self.assertEqual(order[:2], ["lock", "count"])

    def test_the_view_shows_the_limit_message_and_creates_nothing_extra(self):
        self.client.force_login(self.user)
        for _ in range(2):
            self.assertEqual(self.client.post(reverse("chat:create_conversation")).status_code, 302)
        response = self.client.post(reverse("chat:create_conversation"), follow=True)
        self.assertContains(response, "limit of 2 new conversation")
        self.assertEqual(Conversation.all_objects.filter(user=self.user).count(), 2)


@skipUnless(
    connection.features.has_select_for_update and connection.vendor == "postgresql",
    "row locks (and true concurrent transactions) need PostgreSQL; SQLite serialises writers",
)
class ConversationQuotaConcurrencyTests(TransactionTestCase):
    def test_simultaneous_requests_cannot_exceed_the_limit(self):
        # Built here, not read from the seeded plans: a TransactionTestCase flushes every table, so
        # seeded rows may be gone by the time this runs.
        plan = Plan.objects.create(name="RaceTest", sessions_per_day_limit=3)
        user = User.objects.create_user(email="race@example.com", password="pw12345!")
        assign_plan(user, plan)
        attempts = 12
        barrier = threading.Barrier(attempts)
        created, refused, errors = [], [], []

        def worker():
            try:
                barrier.wait(timeout=10)
                created.append(create_conversation_within_quota(user).pk)
            except UsageLimitExceeded:
                refused.append(1)
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(created), 3)
        self.assertEqual(len(refused), attempts - 3)
        self.assertEqual(Conversation.all_objects.filter(user=user).count(), 3)
