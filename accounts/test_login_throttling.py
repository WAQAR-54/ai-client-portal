"""Login throttling across the three attack shapes, and what the limiters do when Redis is down.

* one username from one IP      -> django-axes locks the account (already existed)
* one username from MANY IPs    -> the same lock, because axes keys on the username alone
* MANY usernames from one IP    -> NEW: failed logins are counted per IP
The refusal never depends on whether the account exists."""

from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts import rate_limit
from accounts.models import User


def bad_login(client, username, ip="203.0.113.10", password="wrong-password"):
    return client.post(
        reverse("accounts:login"), {"username": username, "password": password}, HTTP_CF_CONNECTING_IP=ip
    )


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        rate_limit._local_counters.clear()
        rate_limit._status.update(degraded_since=None, fallback_hits=0, last_logged=0.0, last_error="")
        self.user = User.objects.create_user(email="real@example.com", password="right-password-1")


@override_settings(AXES_ENABLED=True, LOGIN_IP_FAILURE_LIMIT=5)
class ManyUsernamesOneIpTests(_Base):
    def test_an_ip_that_keeps_failing_across_usernames_is_stopped(self):
        for i in range(5):
            self.assertContains(bad_login(self.client, f"guess{i}@example.com"), "correct email and password")
        blocked = bad_login(self.client, "guess99@example.com")
        self.assertContains(blocked, "Too many login attempts")

    def test_the_stopped_ip_cannot_log_in_even_with_the_right_password(self):
        for i in range(5):
            bad_login(self.client, f"guess{i}@example.com")
        response = bad_login(self.client, "real@example.com", password="right-password-1")
        self.assertContains(response, "Too many login attempts")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_another_ip_is_unaffected(self):
        for i in range(5):
            bad_login(self.client, f"guess{i}@example.com")
        response = bad_login(self.client, "real@example.com", ip="198.51.100.7", password="right-password-1")
        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)

    def test_successful_logins_never_count_toward_the_ip_limit(self):
        for _ in range(12):  # a whole office arriving in the morning
            self.client.logout()
            response = bad_login(self.client, "real@example.com", password="right-password-1")
            self.assertEqual(response.status_code, 302)

    def test_the_answer_is_the_same_whether_or_not_the_account_exists(self):
        for i in range(5):
            bad_login(self.client, f"guess{i}@example.com")
        existing = bad_login(self.client, "real@example.com")
        unknown = bad_login(self.client, "never-registered@example.com")
        self.assertEqual(existing.status_code, unknown.status_code)
        self.assertEqual([str(m) for m in existing.context["messages"]], [str(m) for m in unknown.context["messages"]])

    def test_the_window_expires(self):
        for i in range(5):
            bad_login(self.client, f"guess{i}@example.com")
        cache.clear()  # what the one-hour TTL does
        self.assertContains(bad_login(self.client, "another@example.com"), "correct email and password")


@override_settings(AXES_ENABLED=True, LOGIN_IP_FAILURE_LIMIT=1000)
class OneUsernameManyIpsTests(_Base):
    def test_the_account_locks_no_matter_which_addresses_the_guesses_come_from(self):
        for n in range(5):
            bad_login(self.client, "real@example.com", ip=f"198.51.100.{n + 1}")
        locked = bad_login(self.client, "real@example.com", ip="192.0.2.99", password="right-password-1")
        self.assertEqual(locked.status_code, 429)  # accounts/axes_hooks.py: the lockout page
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_lock_looks_the_same_for_a_real_and_an_unknown_account(self):
        for n in range(5):
            bad_login(self.client, "real@example.com", ip=f"198.51.100.{n + 1}")
            bad_login(self.client, "ghost@example.com", ip=f"198.51.100.{n + 1}")
        real = bad_login(self.client, "real@example.com", ip="192.0.2.50")
        ghost = bad_login(self.client, "ghost@example.com", ip="192.0.2.50")
        self.assertEqual(real.status_code, ghost.status_code)
        self.assertEqual(real.content, ghost.content)


@override_settings(AXES_ENABLED=False, LOGIN_IP_FAILURE_LIMIT=5)
class RedisDownLoginTests(_Base):
    """The login limiters keep working, locally, when the shared cache is unreachable."""

    def down(self):
        boom = ConnectionError("redis down")
        return (
            patch.object(rate_limit.cache, "get", side_effect=boom),
            patch.object(rate_limit.cache, "set", side_effect=boom),
            patch.object(rate_limit.cache, "incr", side_effect=boom),
        )

    def test_ip_failures_are_still_limited_and_a_normal_user_is_not_locked_out(self):
        get, set_, incr = self.down()
        with get, set_, incr:
            for i in range(5):
                self.assertEqual(bad_login(self.client, f"guess{i}@example.com").status_code, 200)
            self.assertContains(bad_login(self.client, "guess9@example.com"), "Too many login attempts")
            ok = bad_login(self.client, "real@example.com", ip="198.51.100.7", password="right-password-1")
            self.assertEqual(ok.status_code, 302)

    def test_the_per_username_limit_still_applies(self):
        get, set_, incr = self.down()
        with get, set_, incr, override_settings(LOGIN_IP_FAILURE_LIMIT=10_000):
            with patch("accounts.views.LOGIN_RATE_LIMIT", 3):
                for _ in range(3):
                    bad_login(self.client, "real@example.com", ip="198.51.100.1")
                self.assertContains(
                    bad_login(self.client, "real@example.com", ip="198.51.100.2"),
                    "Too many login attempts for this account",
                )


class LimiterPolicyTests(SimpleTestCase):
    """SECURITY_CRITICAL / EXPENSIVE / NORMAL when the cache raises."""

    def setUp(self):
        rate_limit._local_counters.clear()
        rate_limit._status.update(degraded_since=None, fallback_hits=0, last_logged=0.0, last_error="")
        boom = ConnectionError("redis down")
        for name in ("get", "set", "incr", "add"):
            patcher = patch.object(rate_limit.cache, name, side_effect=boom)
            patcher.start()
            self.addCleanup(patcher.stop)

    def hits(self, policy, n, limit=3, key="k"):
        return [rate_limit.is_rate_limited(key, limit=limit, window_seconds=60, policy=policy) for _ in range(n)]

    def test_security_critical_and_expensive_fall_back_to_a_local_counter(self):
        for policy in (rate_limit.SECURITY_CRITICAL, rate_limit.EXPENSIVE):
            rate_limit._local_counters.clear()
            self.assertEqual(self.hits(policy, 5), [False, False, False, True, True], policy)

    def test_normal_fails_open(self):
        self.assertEqual(self.hits(rate_limit.NORMAL, 10), [False] * 10)

    def test_keys_are_independent_in_the_fallback(self):
        self.hits(rate_limit.SECURITY_CRITICAL, 5, key="a")
        self.assertFalse(
            rate_limit.is_rate_limited("b", limit=3, window_seconds=60, policy=rate_limit.SECURITY_CRITICAL)
        )

    def test_the_local_window_expires(self):
        self.hits(rate_limit.EXPENSIVE, 5, limit=2)
        with patch.object(rate_limit.time, "monotonic", return_value=rate_limit.time.monotonic() + 61):
            self.assertFalse(rate_limit.is_rate_limited("k", limit=2, window_seconds=60, policy=rate_limit.EXPENSIVE))

    def test_the_local_store_is_bounded(self):
        with patch.object(rate_limit, "_LOCAL_MAX_KEYS", 50):
            for i in range(500):
                rate_limit.is_rate_limited(f"k{i}", limit=3, window_seconds=60, policy=rate_limit.SECURITY_CRITICAL)
        self.assertLessEqual(len(rate_limit._local_counters), 50)

    def test_the_outage_is_visible_and_logged_once_a_minute_without_the_key(self):
        with self.assertLogs("accounts.rate_limit", level="WARNING") as logs:
            self.hits(rate_limit.SECURITY_CRITICAL, 20, key="login:victim@example.com")
        self.assertEqual(len(logs.records), 1)
        self.assertNotIn("victim@example.com", logs.output[0])
        status = rate_limit.limiter_status()
        self.assertIsNotNone(status["degraded_since"])
        self.assertEqual(status["last_error"], "ConnectionError")
        self.assertGreater(status["fallback_hits"], 0)

    def test_recovery_clears_the_degraded_flag(self):
        self.hits(rate_limit.SECURITY_CRITICAL, 1)
        self.assertIsNotNone(rate_limit.limiter_status()["degraded_since"])
        rate_limit._note_cache_ok()
        self.assertIsNone(rate_limit.limiter_status()["degraded_since"])


class CallersDeclareTheirPolicyTests(TestCase):
    def test_the_ai_message_limit_is_expensive_and_survives_a_redis_outage(self):
        from governance.models import Plan
        from governance.plans import assign_plan, check_message_burst_limit

        cache.clear()
        rate_limit._local_counters.clear()
        plan = Plan.objects.get(name="Premium")
        Plan.objects.filter(pk=plan.pk).update(max_messages_per_minute=2)
        user = User.objects.create_user(email="fast@example.com", password="pw12345!")
        assign_plan(user, Plan.objects.get(pk=plan.pk))
        boom = ConnectionError("redis down")
        from governance.limits import UsageLimitExceeded

        with patch.object(rate_limit.cache, "get", side_effect=boom), patch.object(
            rate_limit.cache, "set", side_effect=boom
        ), patch.object(rate_limit.cache, "incr", side_effect=boom):
            check_message_burst_limit(user)
            check_message_burst_limit(user)
            with self.assertRaises(UsageLimitExceeded):
                check_message_burst_limit(user)
