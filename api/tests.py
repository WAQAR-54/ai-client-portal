from django.test import TestCase
from django.urls import reverse


class PingEndpointTests(TestCase):
    def test_ping_returns_ok(self):
        response = self.client.get(reverse("api:ping"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "message": "pong"})

    def test_ping_does_not_require_login(self):
        response = self.client.get(reverse("api:ping"))
        self.assertEqual(response.status_code, 200)


class ReactTestPageTests(TestCase):
    def test_renders_without_login(self):
        response = self.client.get(reverse("react_test"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="root"')
