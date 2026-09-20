"""One signed-in browser per account (accounts/single_session.py): a newer login signs the earlier browser out."""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.contrib.messages import get_messages
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts import single_session
from governance.models import AuditLog

User = get_user_model()
PASSWORD = "Correct-Horse-9-Battery"
EXPLANATION = "signed in on another browser or device"


def make_user(email="one@example.com", **extra):
    return User.objects.create_user(email=email, password=PASSWORD, **extra)


def password_login(client, user):
    response = client.post(reverse("accounts:login"), {"username": user.email, "password": PASSWORD})
    assert response.status_code == 302, response.status_code
    return response


class SingleSessionTests(TestCase):
    def setUp(self):
        cache.clear()  # the per-IP login limiter would otherwise count every test's logins
        self.user = make_user()
        self.first = Client()
        self.second = Client()

    def dashboard(self, client, **extra):
        return client.get(reverse("accounts:dashboard"), **extra)

    def test_a_second_login_signs_the_first_browser_out(self):
        password_login(self.first, self.user)
        self.assertEqual(self.dashboard(self.first).status_code, 200)

        password_login(self.second, self.user)

        response = self.dashboard(self.first)
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(self.dashboard(self.second).status_code, 200, "the newest browser stays signed in")

    def test_the_signed_out_browser_is_told_why(self):
        password_login(self.first, self.user)
        password_login(self.second, self.user)

        response = self.first.get(reverse("accounts:dashboard"), follow=True)

        texts = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertTrue(any(EXPLANATION in text for text in texts), texts)
        self.assertContains(response, EXPLANATION)

    def test_the_signed_out_browser_stays_out(self):
        password_login(self.first, self.user)
        password_login(self.second, self.user)
        self.dashboard(self.first)  # signed out here

        again = self.dashboard(self.first)

        self.assertEqual(again.status_code, 302)
        self.assertIn(reverse("accounts:login"), again["Location"])

    def test_signing_in_again_on_the_same_browser_keeps_it_signed_in(self):
        password_login(self.first, self.user)
        self.first.get(reverse("accounts:logout"))
        password_login(self.first, self.user)
        self.assertEqual(self.dashboard(self.first).status_code, 200)

    def test_signing_back_in_on_the_first_browser_signs_the_second_out(self):
        password_login(self.first, self.user)
        password_login(self.second, self.user)
        self.dashboard(self.first)  # first is out now
        password_login(self.first, self.user)

        self.assertEqual(self.dashboard(self.first).status_code, 200)
        self.assertEqual(self.dashboard(self.second).status_code, 302)

    def test_other_accounts_are_not_affected(self):
        other = make_user("two@example.com")
        other_client = Client()
        password_login(self.first, self.user)
        password_login(other_client, other)
        password_login(self.second, self.user)

        self.assertEqual(self.dashboard(other_client).status_code, 200)

    def test_an_htmx_request_from_a_superseded_browser_gets_hx_redirect(self):
        password_login(self.first, self.user)
        password_login(self.second, self.user)

        response = self.dashboard(self.first, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], reverse("accounts:login"))

    def test_the_superseded_sign_out_is_audited(self):
        password_login(self.first, self.user)
        password_login(self.second, self.user)
        self.dashboard(self.first)

        rows = AuditLog.objects.filter(action_type="auth.session_superseded", target_id=str(self.user.pk))
        self.assertEqual(rows.count(), 1)
        self.assertNotIn(single_session.SESSION_KEY, rows.first().new_value)

    def test_anonymous_requests_are_untouched(self):
        response = Client().get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)

    def test_changing_the_password_does_not_sign_the_user_out_of_their_own_browser(self):
        password_login(self.first, self.user)
        new_password = "Another-Long-Pass-77"

        response = self.first.post(
            reverse("accounts:profile_password"),
            {"old_password": PASSWORD, "new_password1": new_password, "new_password2": new_password},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.dashboard(self.first).status_code, 200)

    def test_the_token_is_random_per_login_and_never_in_a_page(self):
        password_login(self.first, self.user)
        self.user.refresh_from_db()
        token_one = self.user.active_session_token
        password_login(self.second, self.user)
        self.user.refresh_from_db()

        self.assertNotEqual(token_one, self.user.active_session_token)
        self.assertGreaterEqual(len(self.user.active_session_token), 32)
        self.assertNotContains(self.dashboard(self.second), self.user.active_session_token)

    @override_settings(SINGLE_SESSION_PER_USER=False)
    def test_the_switch_turns_it_off(self):
        password_login(self.first, self.user)
        password_login(self.second, self.user)

        self.assertEqual(self.dashboard(self.first).status_code, 200)
        self.assertEqual(self.dashboard(self.second).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.active_session_token, "")


class ExistingSessionsTests(TestCase):
    """Sessions that were already signed in when the feature shipped hold no token."""

    def setUp(self):
        cache.clear()
        self.user = make_user()

    def signed_in_without_token(self):
        client = Client()
        client.force_login(self.user)
        # Emulate a pre-feature session: no token in the session, none on the account.
        session = client.session
        session.pop(single_session.SESSION_KEY, None)
        session.save()
        User.objects.filter(pk=self.user.pk).update(active_session_token="")
        return client

    def test_a_session_without_a_token_is_adopted_and_keeps_working(self):
        client = self.signed_in_without_token()

        self.assertEqual(client.get(reverse("accounts:dashboard")).status_code, 200)
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.active_session_token, "")
        self.assertEqual(client.get(reverse("accounts:dashboard")).status_code, 200)

    def test_a_new_login_then_signs_the_adopted_session_out(self):
        old = self.signed_in_without_token()
        old.get(reverse("accounts:dashboard"))  # adopts
        new = Client()
        password_login(new, self.user)

        self.assertEqual(old.get(reverse("accounts:dashboard")).status_code, 302)
        self.assertEqual(new.get(reverse("accounts:dashboard")).status_code, 200)

    def test_a_session_without_a_token_is_signed_out_once_the_account_has_a_current_one(self):
        old = self.signed_in_without_token()
        new = Client()
        password_login(new, self.user)  # the account now has a token the old session never had

        self.assertEqual(old.get(reverse("accounts:dashboard")).status_code, 302)

    def test_losing_the_adoption_race_compares_against_the_winner(self):
        """Two token-less sessions both read an empty token; only the conditional UPDATE's winner may stay."""
        request = RequestFactory().get("/")
        request.session = {}
        stale = User.objects.get(pk=self.user.pk)  # read while the token was still empty
        User.objects.filter(pk=self.user.pk).update(active_session_token="winner-token-from-another-session")
        request.user = stale

        self.assertFalse(single_session.check_session(request))
        self.assertNotIn(single_session.SESSION_KEY, request.session)
