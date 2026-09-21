"""Global branding (governance/branding.py): Branding 1 / 2 / 3 / Custom, applied to the whole application."""

import io
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import User
from billing.models import Invoice
from chat.models import Conversation
from governance import branding
from governance.models import AuditLog, Plan, SiteBranding
from notifications.models import Notification

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "pw12345!"

VALID_CUSTOM = {
    "preset": "custom",
    "brand_name": "Acme AI",
    "primary": "#d6336c",
    "secondary": "#2b2d42",
    "accent": "#ffd166",
    "light_background": "#fafafa",
    "light_surface": "#ffffff",
    "light_text": "#1b1b1f",
    "light_muted": "#4a4a55",
    "light_border": "#dddde3",
    "dark_background": "#101014",
    "dark_surface": "#18181d",
    "dark_text": "#f2f2f5",
    "dark_muted": "#b0b0bb",
    "dark_border": "#2c2c34",
    "font_primary": "Poppins",
    "font_secondary": "Poppins",
    "google_fonts": "1",
}


def png(size=(40, 12), color=(40, 90, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return SimpleUploadedFile("logo.png", buffer.getvalue(), content_type="image/png")


class BrandingCase(TestCase):
    def setUp(self):
        cache.clear()
        self.superadmin = User.objects.create_user(email="root@corp.io", password=PASSWORD, role=User.Role.SUPERADMIN)
        self.admin = User.objects.create_user(email="admin@corp.io", password=PASSWORD, role=User.Role.ADMIN)
        self.manager = User.objects.create_user(email="mgr@corp.io", password=PASSWORD, role=User.Role.MANAGER)
        self.user = User.objects.create_user(email="user@corp.io", password=PASSWORD)
        self.row = SiteBranding.load()

    def as_user(self, user):
        from django.test import Client

        client = Client()
        client.force_login(user)
        return client

    def set_preset(self, preset, **extra):
        row = SiteBranding.load()
        row.preset = preset
        for key, value in extra.items():
            setattr(row, key, value)
        row.save()
        return row

    def apply(self, data, client=None):
        return (client or self.as_user(self.superadmin)).post(reverse("governance:brand_theme_apply"), data)

    def page(self, user=None, url=None):
        client = self.as_user(user) if user else self.client
        return client.get(url or reverse("accounts:login")).content.decode()


# ---------------------------------------------------------------------------------------------------------------------
class PresetLoadingTests(BrandingCase):
    def test_branding_1_is_the_default_and_adds_nothing_to_the_page(self):
        html = self.page()
        self.assertIn('data-brand="branding_1"', html)
        self.assertNotIn('id="brand-tokens"', html)  # main.css alone renders it: byte-for-byte the old appearance
        self.assertIn(
            "family=Manrope:wght@400;500;600;700;800&amp;family=Space+Grotesk",
            html.replace("&", "&amp;") if "&amp;" not in html else html,
        )
        self.assertNotIn("Urbanist", html)

    def test_branding_1_preset_data_is_exactly_what_main_css_says(self):
        css = (ROOT / "static/css/main.css").read_text(encoding="utf-8")

        def block(start_marker):
            start = css.index(start_marker)
            body = css[css.index("{", start) + 1 :]
            depth, out = 1, ""
            for ch in body:
                depth += ch == "{"
                depth -= ch == "}"
                if depth == 0:
                    break
                out += ch
            return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", out))

        light = block(":root {")
        dark = block(':root[data-theme="dark"] {')

        def norm(value):
            value = re.sub(r"\s+", " ", value.strip().lower())
            short = re.fullmatch(r"#([0-9a-f])([0-9a-f])([0-9a-f])", value)  # #fff == #ffffff
            return "#" + "".join(ch * 2 for ch in short.groups()) if short else value

        for theme, actual in (("light", light), ("dark", dark)):
            exact = branding.BRANDING_1["exact"][theme]
            for name, value in exact.items():
                if name not in actual:
                    continue
                stated = norm(actual[name])
                match = re.fullmatch(r"var\((--[a-z0-9-]+)\)", stated)  # an alias: compare what it resolves to
                if match and match.group(1) in exact:
                    stated = norm(exact[match.group(1)])
                self.assertEqual(stated, norm(value), f"{theme} {name}")
        # and the brand tokens named in the brief resolve to the tokens the app already uses
        self.assertEqual(light["--brand-primary"], "var(--accent)")
        self.assertEqual(light["--brand-secondary"], "var(--secondary)")

    def test_branding_2_is_the_web_host_era_brand_kit(self):
        self.set_preset("branding_2")
        html = self.page(self.user, reverse("chat:chat_home"))  # signed in: the mobile header carries the light logo
        self.assertIn('data-brand="branding_2"', html)
        self.assertIn('id="brand-tokens"', html)
        # the printed hex values of Brand Kit.pdf (pages 7-9)
        for value in (
            "#2c6ef8",
            "#0055a5",
            "#c7fc35",
            "#00172a",
            "#d9d9d9",
            "#151515",
            "#e9eae9",
            "#b3b3b3",
            "#7db5dc",
        ):
            self.assertIn(value, html + branding.render_css(branding.BRANDING_2), value)
        self.assertIn("Web Host Era", html)
        self.assertIn("/static/branding/whe-logo-blue.png", html)
        self.assertIn("/static/branding/whe-logo-white.png", html)
        self.assertIn('"Gilroy"', html)
        self.assertIn('"Mont-Trial"', html)
        self.assertIn("Urbanist", html)  # open fallback for Gilroy (the commercial font is not bundled)

    def test_branding_2_files_exist_and_the_commercial_fonts_are_not_bundled(self):
        for name in ("whe-logo-blue.png", "whe-logo-white.png", "whe-mark.png"):
            self.assertTrue((ROOT / "static/branding" / name).is_file(), name)
        bundled = [
            p.name for p in (ROOT / "static").rglob("*") if p.suffix.lower() in (".woff", ".woff2", ".ttf", ".otf")
        ]
        self.assertFalse([n for n in bundled if "gilroy" in n.lower() or "mont" in n.lower()], bundled)

    def test_branding_3_is_a_different_identity_not_a_recolour(self):
        self.set_preset("branding_3")
        html = self.page()
        self.assertIn('data-brand="branding_3"', html)
        css2, css3 = branding.render_css(branding.BRANDING_2), branding.render_css(branding.BRANDING_3)
        self.assertIn("--accent: #4f3fe0", css3)
        self.assertIn("--radius-sm: 4px", css3)  # crisp, unlike Branding 2's 10px
        self.assertIn("--radius-sm: 10px", css2)
        self.assertIn("Sora", html)
        self.assertIn("modern-mark-light.svg", html)
        b1, b2, b3 = (
            branding.tokens_for(b, "light") for b in (branding.BRANDING_1, branding.BRANDING_2, branding.BRANDING_3)
        )
        self.assertEqual(len({b1["--accent"], b2["--accent"], b3["--accent"]}), 3)
        self.assertEqual(len({b1["--color-bg"], b2["--color-bg"], b3["--color-bg"]}), 3)
        self.assertEqual(len({b1["--sidebar-bg"], b2["--sidebar-bg"], b3["--sidebar-bg"]}), 3)  # three nav treatments
        fonts = {b["fonts"]["primary_stack"] for b in (branding.BRANDING_1, branding.BRANDING_2, branding.BRANDING_3)}
        self.assertEqual(len(fonts), 3)

    def test_custom_branding_loads_its_own_values(self):
        clean, errors = branding.validate_custom(VALID_CUSTOM)
        self.assertEqual(errors, {})
        self.set_preset("custom", custom_config=clean)
        html = self.page()
        self.assertIn('data-brand="custom"', html)
        self.assertIn("--accent: #d6336c", html)
        self.assertIn("Poppins", html)
        self.assertIn("family=Poppins", html)  # loaded from Google Fonts because that was ticked

    def test_a_saved_custom_config_that_is_broken_falls_back_to_branding_1(self):
        self.set_preset("custom", custom_config={"brand_name": "X", "primary": "not-a-color"})
        html = self.page()
        self.assertIn('data-brand="branding_1"', html)


class PersistenceAndScopeTests(BrandingCase):
    def test_apply_persists_across_reload_logout_login_and_other_users(self):
        response = self.apply({"preset": "branding_2"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SiteBranding.load().preset, "branding_2")
        for user in (self.user, self.manager, self.admin, self.superadmin):
            client = self.as_user(user)
            client.logout()
            client.force_login(user)
            self.assertIn(
                'data-brand="branding_2"', client.get(reverse("accounts:dashboard")).content.decode(), user.role
            )
        self.assertIn('data-brand="branding_2"', self.page())  # anonymous visitors, too

    def test_it_changes_the_whole_application_at_once(self):
        self.set_preset("branding_3")
        pages = [
            (self.user, reverse("chat:chat_home")),
            (self.user, reverse("billing:my_plans")),
            (self.superadmin, reverse("governance:dashboard")),
            (self.superadmin, reverse("governance:media")),
            (self.superadmin, reverse("governance:brand_theme")),
            (None, reverse("accounts:login")),
            (None, reverse("accounts:password_reset_request")),
        ]
        for user, url in pages:
            with self.subTest(url=url):
                html = self.page(user, url)
                self.assertIn('data-brand="branding_3"', html)
                self.assertIn("--accent: #4f3fe0", html)

    def test_light_and_dark_are_both_generated_from_the_same_brand(self):
        css = branding.render_css(branding.BRANDING_2)
        self.assertIn("@media (prefers-color-scheme: dark)", css)
        self.assertIn(':root[data-theme="dark"]', css)
        light, dark = branding.tokens_for(branding.BRANDING_2, "light"), branding.tokens_for(
            branding.BRANDING_2, "dark"
        )
        self.assertEqual(light["--accent"], dark["--accent"])  # one brand ...
        self.assertNotEqual(light["--color-bg"], dark["--color-bg"])  # ... two surfaces
        self.assertEqual(dark["--color-bg"], "#00172a")
        for preset in ("branding_2", "branding_3"):
            self.set_preset(preset)
            self.assertEqual(self.as_user(self.user).get(reverse("chat:chat_home")).status_code, 200)

    def test_an_explicit_light_or_dark_choice_still_wins_over_the_branding(self):
        self.set_preset("branding_2")
        self.user.theme_preference = "dark"
        self.user.save(update_fields=["theme_preference"])
        html = self.as_user(self.user).get(reverse("chat:chat_home")).content.decode()
        self.assertIn('data-theme="dark"', html)
        self.assertIn('data-brand="branding_2"', html)  # theme and branding are independent


class CustomBrandNameTests(BrandingCase):
    def custom(self, name="Acme AI"):
        clean, _errors = branding.validate_custom({**VALID_CUSTOM, "brand_name": name})
        self.set_preset("custom", custom_config=clean)

    def test_the_name_appears_in_title_sidebar_login_and_admin_pages(self):
        self.custom("Acme AI")
        login = self.page()
        self.assertIn("<title>Log in — Acme AI</title>", login)
        self.assertIn("Acme AI", re.search(r'<div class="brand-mark">(.*?)</div>', login, re.S).group(1))
        chat = self.page(self.user, reverse("chat:chat_home"))
        self.assertIn("Acme AI", re.search(r'<a class="brand"[^>]*>(.*?)</a>', chat, re.S).group(1))
        self.assertNotIn(">AI Client Portal<", chat)

    def test_the_name_is_used_in_email_subjects_and_the_email_header(self):
        self.custom("Acme AI")
        self.assertEqual(branding.brand_name(), "Acme AI")
        self.assertEqual(branding.localize_product_name("Welcome to AI Client Portal"), "Welcome to Acme AI")
        html = self.render_email()
        self.assertIn("Acme AI", html)

    def test_the_default_name_still_comes_from_settings_branding_for_branding_1(self):
        row = self.set_preset("branding_1", site_name="WHE AI")
        self.assertIn("WHE AI", self.page())
        self.assertEqual(branding.brand_name(), row.site_name)

    def render_email(self):
        notification = Notification.objects.create(
            user=self.user,
            notification_type="model_sync_available",
            title="Models updated",
            body="New models are available.",
        )
        from notifications.tasks import send_notification_email

        mail.outbox = []
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            send_notification_email(notification.id)
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0].alternatives[0][0]


class ValidationTests(BrandingCase):
    def test_colors_must_be_six_digit_hex(self):
        for bad in (
            "red",
            "#12",
            "#12345",
            "#gggggg",
            "rgb(0,0,0)",
            "javascript:alert(1)",
            "#fff; } body { display:none",
            "url(x)",
            "",
        ):
            with self.subTest(value=bad):
                _clean, errors = branding.validate_custom({**VALID_CUSTOM, "primary": bad})
                self.assertIn("primary", errors)
        clean, errors = branding.validate_custom({**VALID_CUSTOM, "primary": "#ABC"})
        self.assertEqual((clean["primary"], errors), ("#aabbcc", {}))

    def test_fonts_and_names_cannot_carry_css_or_html(self):
        for bad in ("Arial;}body{display:none", "Inter, serif", "a" * 60, "Bad<Font>", "x{y}", 'x"y', ""):
            with self.subTest(font=bad):
                _clean, errors = branding.validate_custom({**VALID_CUSTOM, "font_primary": bad})
                self.assertIn("font_primary", errors)
        for bad in ("<script>alert(1)</script>", "Name {x}", "a\\b", "x" * 61, "   "):
            with self.subTest(name=bad):
                _clean, errors = branding.validate_custom({**VALID_CUSTOM, "brand_name": bad})
                self.assertIn("brand_name", errors)
        clean, errors = branding.validate_custom(
            {**VALID_CUSTOM, "font_primary": "Open Sans", "font_secondary": "Space-Grotesk"}
        )
        self.assertEqual(errors, {})

    def test_the_generated_css_only_contains_validated_values(self):
        clean, _errors = branding.validate_custom(VALID_CUSTOM)
        css = branding.render_css(branding.custom_brand(clean))
        self.assertNotIn("<", css)
        self.assertNotIn("</style", css.lower())
        declaration = r"--[a-z0-9-]+: [^;<>{}]+;"
        structure = (
            r"color-scheme: dark;|:root(?:\[data-theme=\"dark\"\]|:not\(\[data-theme=\"light\"\]\))? \{"
            r"|@media \(prefers-color-scheme: dark\) \{|\}|::selection \{[^<>]*\}"
        )
        allowed = re.compile(rf"^\s*(?:{declaration}|{structure})\s*$")
        for line in css.splitlines():
            self.assertRegex(line, allowed)

    def test_invalid_input_is_rejected_and_nothing_is_saved(self):
        before = SiteBranding.load()
        response = self.apply({**VALID_CUSTOM, "primary": "purple", "brand_name": "<b>x</b>"})
        self.assertEqual(response.status_code, 200)  # the form again, with the errors
        self.assertContains(response, "6-digit hex")
        after = SiteBranding.load()
        self.assertEqual(
            (after.preset, after.custom_config, after.version), (before.preset, before.custom_config, before.version)
        )

    def test_an_unknown_preset_is_rejected(self):
        response = self.apply({"preset": "branding_9"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SiteBranding.load().preset, "branding_1")

    def test_logo_uploads_are_checked_for_type_and_size(self):
        bad_type = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
        response = self.apply({**VALID_CUSTOM, "logo_light": bad_type})
        self.assertContains(response, "valid image")
        big = SimpleUploadedFile("big.png", b"0" * (2 * 1024 * 1024 + 1), content_type="image/png")
        response = self.apply({**VALID_CUSTOM, "logo_dark": big})
        self.assertContains(response, "under 2 MB")
        self.assertEqual(SiteBranding.load().preset, "branding_1")

    def test_logos_are_saved_and_shown_in_light_and_dark(self):
        response = self.apply({**VALID_CUSTOM, "logo_light": png(), "logo_dark": png(color=(240, 240, 240))})
        self.assertEqual(response.status_code, 302, getattr(response, "content", b"")[:300])
        html = self.page(self.user, reverse("chat:chat_home"))
        self.assertIn("brand-logo-light", html)
        self.assertIn("brand-logo-dark", html)
        row = SiteBranding.load()
        self.assertTrue(row.custom_logo_light and row.custom_logo_dark)
        row.custom_logo_light.delete(save=False)
        row.custom_logo_dark.delete(save=False)


class AccessibilityTests(BrandingCase):
    def test_every_preset_meets_wcag_aa_for_its_key_pairs_in_both_themes(self):
        for brand in (branding.BRANDING_1, branding.BRANDING_2, branding.BRANDING_3):
            report = branding.contrast_report(brand)
            self.assertEqual({r["theme"] for r in report}, {"light", "dark"})
            self.assertEqual(branding.contrast_warnings(brand), [], brand["key"])
            unaccepted = [r for r in report if not r["ok"] and not r["accepted"]]
            self.assertEqual(unaccepted, [], brand["key"])
        # the ONE known shortfall is documented, not hidden: Branding 2's white-on-Brand-Kit-blue button label
        accepted = [r for r in branding.contrast_report(branding.BRANDING_2) if r["accepted"]]
        self.assertEqual({r["label"] for r in accepted}, {"Primary button label"})
        self.assertTrue(all(4.4 <= r["ratio"] < 4.5 for r in accepted), accepted)
        self.assertEqual([r for r in branding.contrast_report(branding.BRANDING_1) if r["accepted"]], [])
        self.assertEqual([r for r in branding.contrast_report(branding.BRANDING_3) if r["accepted"]], [])

    def poor_custom(self):
        return {
            **VALID_CUSTOM,
            "light_text": "#bbbbbb",
            "light_muted": "#cccccc",
            "light_background": "#ffffff",
            "primary": "#ffff00",
        }

    def test_poor_contrast_is_reported_in_the_preview_and_the_colors_are_not_changed(self):
        response = self.as_user(self.superadmin).post(reverse("governance:brand_theme_preview"), self.poor_custom())
        self.assertContains(response, "hard to read")
        self.assertContains(response, 'name="acknowledge_contrast"')
        self.assertContains(response, "#bbbbbb")  # the chosen color is shown as chosen

    def test_apply_is_blocked_until_the_warning_is_acknowledged_then_applies_the_colors_unchanged(self):
        response = self.apply(self.poor_custom())
        self.assertContains(response, "hard to read")
        self.assertEqual(SiteBranding.load().preset, "branding_1")
        response = self.apply({**self.poor_custom(), "acknowledge_contrast": "1"})
        self.assertEqual(response.status_code, 302)
        row = SiteBranding.load()
        self.assertEqual(row.preset, "custom")
        self.assertEqual(row.custom_config["light"]["text"], "#bbbbbb")
        self.assertEqual(row.custom_config["primary"], "#ffff00")

    def test_the_preview_of_a_preset_reports_that_every_pair_passes(self):
        response = self.as_user(self.superadmin).post(
            reverse("governance:brand_theme_preview"), {"preset": "branding_2"}
        )
        self.assertContains(response, "meet 4.5")
        self.assertContains(response, "data-preview-theme")
        self.assertContains(response, ".brand-preview")

    def test_preview_saves_nothing(self):
        before = SiteBranding.load().version
        self.as_user(self.superadmin).post(reverse("governance:brand_theme_preview"), VALID_CUSTOM)
        after = SiteBranding.load()
        self.assertEqual((after.preset, after.version, after.custom_config), ("branding_1", before, {}))


class PermissionTests(BrandingCase):
    URLS = (
        ("get", "governance:brand_theme"),
        ("post", "governance:brand_theme_preview"),
        ("post", "governance:brand_theme_apply"),
        ("post", "governance:brand_theme_reset_custom"),
        ("get", "governance:brand_maintenance_preview"),
    )

    def test_only_a_superadmin_can_reach_or_change_anything(self):
        for user in (self.user, self.manager, self.admin):
            client = self.as_user(user)
            for method, name in self.URLS:
                with self.subTest(role=user.role, url=name):
                    self.assertEqual(getattr(client, method)(reverse(name), {"preset": "branding_2"}).status_code, 403)
        self.assertEqual(SiteBranding.load().preset, "branding_1")

    def test_anonymous_visitors_are_sent_to_login(self):
        for method, name in self.URLS:
            with self.subTest(url=name):
                response = getattr(self.client, method)(reverse(name), {"preset": "branding_2"})
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(SiteBranding.load().preset, "branding_1")

    def test_a_normal_user_still_consumes_the_active_branding(self):
        self.set_preset("branding_2")
        self.assertIn('data-brand="branding_2"', self.page(self.user, reverse("chat:chat_home")))

    def test_applying_is_audited(self):
        self.apply({"preset": "branding_3"})
        self.assertTrue(AuditLog.objects.filter(action_type="branding.theme_apply", new_value="branding_3").exists())

    def test_reset_custom_clears_the_config_and_returns_to_branding_1_if_it_was_applied(self):
        self.apply({**VALID_CUSTOM, "logo_light": png()})
        self.assertEqual(SiteBranding.load().preset, "custom")
        response = self.as_user(self.superadmin).post(reverse("governance:brand_theme_reset_custom"))
        self.assertEqual(response.status_code, 302)
        row = SiteBranding.load()
        self.assertEqual((row.preset, row.custom_config, bool(row.custom_logo_light)), ("branding_1", {}, False))


class SafetyTests(BrandingCase):
    def test_switching_branding_changes_only_the_branding_row(self):
        plan = Plan.objects.create(name="Plan X")
        invoice = Invoice.objects.create(
            recipient_user=self.user,
            plan=plan,
            issue_date=timezone.localdate(),
            due_date=timezone.localdate() + timedelta(days=7),
            currency="USD",
            subtotal=Decimal("10"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("10"),
        )
        conversation = Conversation.objects.create(user=self.user, title="Keep me")
        snapshot = lambda: (  # noqa: E731
            list(
                User.objects.order_by("id").values_list(
                    "id", "email", "role", "is_active", "department_id", "plan_id" if hasattr(User, "plan_id") else "id"
                )
            ),
            list(Plan.objects.order_by("id").values_list("id", "name")),
            list(Invoice.objects.order_by("id").values_list("id", "total", "status")),
            list(Conversation.objects.order_by("id").values_list("id", "title")),
        )
        before = snapshot()
        for preset in ("branding_2", "branding_3", "custom", "branding_1"):
            self.apply(VALID_CUSTOM if preset == "custom" else {"preset": preset})
        self.assertEqual(snapshot(), before)
        del invoice, conversation

    def test_branding_never_breaks_a_page_if_the_cache_is_down(self):
        from unittest.mock import patch

        self.set_preset("branding_2")
        with patch.object(cache, "get", side_effect=OSError("down")), patch.object(
            cache, "set", side_effect=OSError("down")
        ):
            self.assertIn('data-brand="branding_2"', self.page())


class CacheTests(BrandingCase):
    def test_changing_the_branding_invalidates_the_cached_css_immediately(self):
        self.apply({"preset": "branding_2"})
        self.assertIn("--accent: #2c6ef8", self.page())  # rendered (and cached) under Branding 2
        self.assertIn("--accent: #2c6ef8", self.page())  # served from the cache
        self.apply({"preset": "branding_3"})
        html = self.page()
        self.assertIn("--accent: #4f3fe0", html)
        self.assertNotIn("--accent: #2c6ef8", html)
        self.apply({"preset": "branding_1"})
        self.assertNotIn('id="brand-tokens"', self.page())

    def test_every_save_bumps_the_version_that_keys_the_cache(self):
        before = SiteBranding.load().version
        row = SiteBranding.load()
        row.site_name = "Renamed"
        row.save()
        self.assertGreater(SiteBranding.load().version, before)

    def test_editing_custom_values_shows_up_at_once(self):
        self.apply(VALID_CUSTOM)
        self.assertIn("--accent: #d6336c", self.page())
        self.apply({**VALID_CUSTOM, "primary": "#0a7f5a"})
        html = self.page()
        self.assertIn("--accent: #0a7f5a", html)
        self.assertNotIn("--accent: #d6336c", html)


class DocumentsTests(BrandingCase):
    def make_invoice(self):
        plan = Plan.objects.create(name="Public Plan")
        return Invoice.objects.create(
            recipient_user=self.user,
            plan=plan,
            issue_date=timezone.localdate(),
            due_date=timezone.localdate() + timedelta(days=7),
            currency="USD",
            subtotal=Decimal("50"),
            tax_rate=Decimal("0"),
            tax_amount=Decimal("0"),
            total=Decimal("50"),
        )

    def test_the_email_layout_uses_the_active_branding(self):
        notification = Notification.objects.create(
            user=self.user, notification_type="model_sync_available", title="Hi", body="Body"
        )
        from notifications.tasks import send_notification_email

        html = {}
        for preset in ("branding_1", "branding_2", "branding_3"):
            self.set_preset(preset)
            mail.outbox = []
            with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
                send_notification_email(notification.id)
            html[preset] = mail.outbox[0].alternatives[0][0]
        self.assertIn("#00aef0", html["branding_1"])
        self.assertIn("#2c6ef8", html["branding_2"])
        self.assertNotIn("#00aef0", html["branding_2"])
        self.assertIn("#4f3fe0", html["branding_3"])
        self.assertIn("Web Host Era", html["branding_2"])  # the brand name in the header (Branding 2's own)
        self.assertNotIn("#12172b", html["branding_2"])  # the old hard-coded heading colour is gone
        # one layout: the structure is identical, only values differ
        self.assertIn("Hi", html["branding_2"])

    def test_every_account_and_billing_email_uses_the_active_branding(self):
        from billing.emails import send_invoice_email

        invoice = self.make_invoice()
        self.set_preset("branding_2")
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            mail.outbox = []
            send_invoice_email(invoice)
        body = mail.outbox[0].alternatives[0][0] if mail.outbox[0].alternatives else mail.outbox[0].body
        self.assertIn("#2c6ef8", body)
        self.assertNotIn("#00aef0", body)
        self.assertIn("Web Host Era", mail.outbox[0].subject)

    def test_the_invoice_page_pdf_and_share_link_use_the_active_branding(self):
        from billing.pdf import render_invoice_pdf

        invoice = self.make_invoice()
        self.set_preset("branding_2")
        page = self.client.get(
            reverse("billing:public_invoice", kwargs={"token": invoice.share_token})
        ).content.decode()
        self.assertIn("#2c6ef8", page)
        self.assertNotIn("#00aef0", page)
        self.assertIn("Web Host Era", page)
        self.assertTrue(render_invoice_pdf(invoice).startswith(b"%PDF"))
        for preset in ("branding_1", "branding_3"):
            self.set_preset(preset)
            self.assertTrue(render_invoice_pdf(invoice).startswith(b"%PDF"), preset)
        self.assertIn(
            "#4f3fe0",
            self.client.get(reverse("billing:public_invoice", kwargs={"token": invoice.share_token})).content.decode(),
        )

    def test_the_maintenance_page_uses_the_active_branding(self):
        client = self.as_user(self.superadmin)
        expected = {"branding_1": None, "branding_2": "#2c6ef8", "branding_3": "#4f3fe0"}
        for preset, accent in expected.items():
            self.set_preset(preset)
            html = client.get(reverse("governance:brand_maintenance_preview")).content.decode()
            self.assertIn(f'data-brand="{preset}"', html)
            self.assertIn("We&#x27;ll be right back", html.replace("'", "&#x27;"))
            if accent:
                self.assertIn(f"--accent: {accent}", html)
        self.set_preset("branding_2")
        self.assertIn("Web Host Era", client.get(reverse("governance:brand_maintenance_preview")).content.decode())
        clean, _e = branding.validate_custom(VALID_CUSTOM)
        self.set_preset("custom", custom_config=clean)
        html = client.get(reverse("governance:brand_maintenance_preview")).content.decode()
        self.assertIn("Acme AI", html)
        self.assertIn("--accent: #d6336c", html)

    def test_the_error_and_maintenance_pages_can_render_from_the_cache_alone(self):
        from django.template.loader import render_to_string

        self.set_preset("branding_2")
        self.page()  # any normal page render records the branding
        for template in ("500.html", "maintenance.html"):
            html = render_to_string(template)  # no request, no context processors, no database
            self.assertIn("--accent: #2c6ef8", html, template)
        self.assertIn("Web Host Era", render_to_string("maintenance.html"))
        cache.clear()
        self.assertNotIn("#2c6ef8", render_to_string("500.html"))  # nothing known yet: the default look

    def test_the_login_page_titles_and_dashboard_chart_hook_follow_the_branding(self):
        self.set_preset("branding_2")
        dashboard = self.page(self.superadmin, reverse("governance:dashboard"))
        self.assertIn('document.documentElement.dataset.brand !== "branding_1"', dashboard)
        self.assertIn("Web Host Era", self.page())


class PreviewAndLogoContextTests(BrandingCase):
    def test_the_preview_fragment_does_not_start_with_style_or_link(self):
        """htmx parses a response as a document: a leading <style>/<link> would be hoisted into <head> and dropped,
        and the preview would silently render in the CURRENT branding (found in a real browser, not by a unit test)."""
        response = self.as_user(self.superadmin).post(
            reverse("governance:brand_theme_preview"), {"preset": "branding_3"}
        )
        html = response.content.decode().lstrip()
        self.assertTrue(html.startswith("<div"), html[:80])
        self.assertIn("<style>", html)
        self.assertLess(html.index("<div"), html.index("<style>"))

    def test_branding_2_uses_its_white_logo_on_the_always_dark_sidebar_and_the_login_panel(self):
        self.set_preset("branding_2")
        chat = self.page(self.user, reverse("chat:chat_home"))
        sidebar = re.search(r'<a class="brand"[^>]*>(.*?)</a>', chat, re.S).group(1)
        self.assertIn("whe-logo-white.png", sidebar)
        self.assertNotIn("whe-logo-blue.png", sidebar)
        login = re.search(r'<div class="brand-mark">(.*?)</div>', self.page(), re.S).group(1)
        self.assertIn("whe-logo-white.png", login)
        self.assertNotIn("whe-logo-blue.png", login)

    def test_a_light_sidebar_branding_keeps_the_light_dark_logo_pair(self):
        clean, _errors = branding.validate_custom(VALID_CUSTOM)
        row = self.set_preset("custom", custom_config=clean)
        row.custom_logo_light.save("l.png", png(), save=False)
        row.custom_logo_dark.save("d.png", png(color=(250, 250, 250)), save=True)
        try:
            sidebar = re.search(
                r'<a class="brand"[^>]*>(.*?)</a>', self.page(self.user, reverse("chat:chat_home")), re.S
            ).group(1)
            self.assertIn("brand-logo-light", sidebar)
            self.assertIn("brand-logo-dark", sidebar)
        finally:
            row.custom_logo_light.delete(save=False)
            row.custom_logo_dark.delete(save=False)


class ConsolePagesTests(BrandingCase):
    """Domain Generator and Code Playground are role-gated tools that end users work in (client-facing surfaces), so
    they consume the global tokens like every other page instead of carrying a palette of their own."""

    URLS = (reverse_lazy_home := ("domaingen:home", "playground:home"))

    def page_of(self, name):
        return self.as_user(self.superadmin).get(reverse(name)).content.decode()

    def test_they_carry_the_full_token_set_of_the_active_branding(self):
        for name in self.URLS:
            for preset, accent, bg in (
                ("branding_1", "#00aef0", "#f1f0eb"),
                ("branding_2", "#2c6ef8", "#ffffff"),
                ("branding_3", "#4f3fe0", "#f4f6fb"),
            ):
                with self.subTest(page=name, preset=preset):
                    self.set_preset(preset)
                    html = self.page_of(name)
                    self.assertIn(f"--accent: {accent}", html)
                    self.assertIn(f"--color-bg: {bg}", html)
                    self.assertIn("@media (prefers-color-scheme: dark)", html)  # light AND dark, like the app
                    self.assertIn(
                        f'href="{branding.PRESETS[preset]["fonts"]["google_url"].replace("&", "&amp;")}"', html
                    )

    def test_their_old_private_palette_is_gone(self):
        self.set_preset("branding_2")
        for name in self.URLS:
            html = self.page_of(name)
            for old in (
                "#00D9FF",
                "#0B0D12",
                "#12151C",
                "#F2F3F5",
                "#0A2E38",
                "#3ECF8E",
                "#F2B84B",
                "'Inter'",
                "'Space Grotesk'",
            ):
                self.assertNotIn(old, html, (name, old))
            self.assertIn("var(--color-bg)", html)
            self.assertIn("var(--accent-text)", html)
            self.assertIn("var(--font-sans)", html)

    def test_custom_branding_reaches_them(self):
        clean, _e = branding.validate_custom(VALID_CUSTOM)
        self.set_preset("custom", custom_config=clean)
        for name in self.URLS:
            html = self.page_of(name)
            self.assertIn("--accent: #d6336c", html)
            self.assertIn("Acme AI", html)  # the tab title
