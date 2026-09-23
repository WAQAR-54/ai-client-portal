"""The four public legal pages (templates/legal/): they load without signing in, are complete, say only what the
application really does, and are reachable from the existing navigation."""

import re
from pathlib import Path

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from billing.models import REFUND_WINDOW_DAYS
from governance.models import SiteBranding

PAGES = {"privacy": 31, "terms": 42, "refund": 24, "ai_usage": 24}
PAGES["refund"] = 22  # the requested outline has 22 sections
URLS = {"privacy": "legal:privacy", "terms": "legal:terms", "refund": "legal:refund", "ai_usage": "legal:ai_usage"}
PLACEHOLDER = "[BUSINESS / LEGAL CONFIRMATION REQUIRED]"


def text_of(response):
    html = response.content.decode()
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class LegalPageTests(TestCase):
    def setUp(self):
        cache.clear()

    def page(self, key):
        return self.client.get(reverse(URLS[key]))

    def test_all_four_load_for_an_anonymous_visitor(self):
        for key in URLS:
            with self.subTest(page=key):
                response = self.page(key)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'data-legal="{key}"')

    def test_each_page_has_its_full_outline_and_unique_section_anchors(self):
        for key, count in PAGES.items():
            with self.subTest(page=key):
                html = self.page(key).content.decode()
                ids = re.findall(r'<section id="([a-z0-9-]+)"><h2>', html)
                self.assertEqual(len(ids), count)
                self.assertEqual(len(ids), len(set(ids)), "duplicate anchors")
                self.assertGreater(len(text_of(self.page(key)).split()), 1200 if key != "refund" else 1000)

    def test_global_legal_information_is_on_every_page_and_nothing_is_invented(self):
        for key in URLS:
            with self.subTest(page=key):
                response = self.page(key)
                for label in ("Service", "Legal entity", "Effective date", "Last updated", "Version", "Contact"):
                    self.assertContains(response, f"<dt>{label}</dt>")
                self.assertContains(response, PLACEHOLDER)
                self.assertContains(response, "has not yet been reviewed or approved by legal counsel")

    def test_the_ai_page_uses_the_required_business_wording(self):
        text = text_of(self.page("ai_usage"))
        for phrase in (
            "We provide our own software platform and AI-powered functionality "
            "through integrations with third-party AI service providers.",
            "do not sell, transfer, sublicense, or provide users with third-party provider API keys "
            "or provider accounts",
            "Users access supported AI models through our platform.",
        ):
            self.assertIn(phrase, text)

    def test_only_the_providers_the_application_supports_are_named(self):
        text = text_of(self.page("ai_usage"))
        for name in ("Anthropic", "OpenAI", "Google", "xAI", "DeepSeek"):
            self.assertIn(name, text)
        for other in ("Mistral", "Cohere", "Llama", "Perplexity", "Microsoft Copilot"):
            self.assertNotIn(other, text)
        for page in URLS:
            self.assertNotRegex(text_of(self.page(page)).lower(), r"\bresale\b|\bresell(er|ing)? (of )?(ai|models)")

    def test_ai_processing_is_explained_and_no_unsupported_guarantee_is_made(self):
        for key in ("privacy", "ai_usage", "terms"):
            text = text_of(self.page(key)).lower()
            for claim in (
                r"zero[- ]data[- ]retention",
                r"no logging",
                r"does not keep any",
                r"never train",
                r"do not train on your",
                r"no international transfers",
                r"money[- ]back guarantee",
                r"30[- ]days? (money|refund|guarantee|return)",
                r"we do not use your data to train",
            ):
                self.assertIsNone(re.search(claim, text), f"{key}: unsupported claim matching {claim!r}")
        privacy = text_of(self.page("privacy"))
        self.assertIn("your prompt, the relevant earlier messages", privacy)
        self.assertIn("sent to the third-party AI provider", privacy)

    def test_the_refund_page_matches_what_the_application_really_enforces(self):
        text = text_of(self.page("refund"))
        self.assertEqual(REFUND_WINDOW_DAYS, 7)
        self.assertIn("7 days", text)
        self.assertIn("full refund", text)
        self.assertIn("no card checkout", text)
        # no promise the application does not keep
        for claim in ("14 days", "60 days", "90 days", "automatic refund of any", "guarantee"):
            self.assertNotIn(claim, text.lower().replace("no guarantee", ""))
        self.assertGreaterEqual(self.page("refund").content.decode().count("legal-todo"), 10)

    def test_the_contact_line_uses_the_configured_support_address_else_a_marker(self):
        self.assertContains(self.page("privacy"), "privacy / support contact e-mail address")
        with override_settings(SUPPORT_EMAIL="help@corp.io"):
            for key in URLS:
                self.assertContains(self.page(key), 'href="mailto:help@corp.io"')

    def test_no_secret_or_private_data_appears(self):
        User.objects.create_user(email="someone@private.io", password="pw12345!Strong")
        from django.conf import settings

        for key in URLS:
            body = self.page(key).content.decode()
            self.assertNotIn("someone@private.io", body)
            self.assertNotIn(settings.SECRET_KEY, body)
            self.assertNotRegex(body, r"sk-[A-Za-z0-9]{16,}")


class LegalNavigationTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_all_four_footer_links_are_on_pricing_and_every_legal_page(self):
        hrefs = [reverse(name) for name in URLS.values()]
        for url in (reverse("billing:public_pricing"), *hrefs):
            body = self.client.get(url).content.decode()
            for href in hrefs:
                self.assertIn(f'href="{href}"', body, f"{url} lacks a link to {href}")

    def test_sign_in_and_sign_up_show_a_subtle_agreement_line_not_four_buttons(self):
        for url in (reverse("accounts:login"), reverse("accounts:signup")):
            body = self.client.get(url).content.decode()
            self.assertIn(f'href="{reverse("legal:terms")}"', body)
            self.assertIn(f'href="{reverse("legal:privacy")}"', body)
            self.assertNotIn(f'href="{reverse("legal:refund")}"', body)
            self.assertNotIn(f'href="{reverse("legal:ai_usage")}"', body)

    def test_legal_links_are_not_in_the_signed_in_sidebar(self):
        user = User.objects.create_user(email="reader@corp.io", password="pw12345!Strong")
        client = Client()
        client.force_login(user)

        body = client.get(reverse("accounts:dashboard")).content.decode()
        sidebar_match = re.search(r'<aside[^>]*class="[^"]*\bapp-sidebar\b[^"]*"[^>]*>(.*?)</aside>', body, re.DOTALL)
        self.assertIsNotNone(sidebar_match, "Signed-in sidebar was not found")
        sidebar = sidebar_match.group(1)

        for name in URLS.values():
            with self.subTest(page=name):
                self.assertNotIn(
                    f'href="{reverse(name)}"', sidebar, f"Legal link {name} must not appear in the signed-in sidebar"
                )
                self.assertEqual(client.get(reverse(name)).status_code, 200)

    def test_every_internal_link_on_every_legal_page_resolves(self):
        for name in URLS.values():
            body = self.client.get(reverse(name)).content.decode()
            main = body.split('class="legal"', 1)[1]
            for href in set(re.findall(r'href="(/[^"#]*)"', main)):
                if href.startswith(("/static/", "/media/")):
                    continue
                self.assertIn(self.client.get(href).status_code, (200, 302), f"{name}: broken link {href}")
            for anchor in re.findall(r'href="#([^"]+)"', main):
                self.assertIn(f'id="{anchor}"', body, f"{name}: missing anchor #{anchor}")

    def test_external_links_are_only_provider_policy_pages_and_open_safely(self):
        body = self.client.get(reverse("legal:ai_usage")).content.decode()
        links = re.findall(r'<a href="(https?://[^"]+)"([^>]*)>', body)
        self.assertGreaterEqual(len(links), 5)
        for url, attrs in links:
            self.assertIn('rel="noopener noreferrer"', attrs, url)


class LegalDesignTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_renders_under_every_branding_preset(self):
        for preset in ("branding_1", "branding_2", "branding_3"):
            SiteBranding.objects.update_or_create(pk=1, defaults={"preset": preset, "version": 700 + len(preset)})
            cache.clear()
            response = self.client.get(reverse("legal:terms"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, f'data-brand="{preset}"')

    def test_the_legal_css_uses_tokens_only(self):
        css = (Path(__file__).resolve().parent.parent / "static" / "css" / "main.css").read_text(encoding="utf-8")
        block = css[css.index("Legal pages (privacy, terms, refund, AI usage)") :]
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block), [])
