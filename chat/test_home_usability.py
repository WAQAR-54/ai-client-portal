"""Chat home usability: it must scroll, the ask box must stay reachable, and
"Compare" must do something from the home (it used to be a silent no-op there
because the pick-two-models screen only exists inside a conversation)."""

from django.test import override_settings
from django.urls import reverse

from chat.models import Conversation
from chat.test_live_intelligence import LiveIntelligenceViewBase


@override_settings(LIVE_INTELLIGENCE_ENABLED=True)
class ChatHomeUsabilityTests(LiveIntelligenceViewBase):
    def _home(self):
        return self.client.get(reverse("chat:chat_home"))

    def test_the_home_content_scrolls_and_the_ask_box_sits_outside_the_scroll_area(self):
        """.chat-panel is overflow:hidden, so taller-than-the-window content was
        simply clipped (and the ask box pushed out of view) until it got its own
        scroll container."""
        html = self._home().content.decode()
        scroll_open = html.index('class="chat-home-scroll"')
        hero = html.index('class="chat-hero"')
        intel = html.index('id="chat-intel"')
        ask_box = html.index('class="chat-composer-wrap"')
        self.assertLess(scroll_open, hero)
        self.assertLess(hero, intel)
        self.assertLess(intel, ask_box)
        # the wrapper is closed before the ask box: two closing divs (hero, then the wrapper)
        between = html[intel:ask_box]
        self.assertGreaterEqual(between.count("</div>"), 2)

    def test_the_compare_card_opens_compare_mode_instead_of_just_prefilling_text(self):
        response = self._home()
        self.assertTrue(response.context["can_select_model"])
        self.assertContains(response, 'name="compare" value="1"')
        self.assertNotContains(response, "Compare how two models answer the same question")

    def test_the_sidebar_compare_link_has_a_form_to_start_from_the_home(self):
        html = self._home().content.decode()
        self.assertIn('id="compare-start-form"', html)
        self.assertIn('document.getElementById("compare-start-form")', html)

    def test_starting_compare_creates_a_conversation_and_lands_on_the_compare_screen(self):
        before = Conversation.objects.filter(user=self.user).count()
        response = self.client.post(reverse("chat:create_conversation"), {"compare": "1"})
        self.assertEqual(Conversation.objects.filter(user=self.user).count(), before + 1)
        conversation = Conversation.objects.filter(user=self.user).latest("id")
        self.assertRedirects(
            response,
            reverse("chat:chat_conversation", args=[conversation.id]) + "?compare=1",
            fetch_redirect_response=False,
        )

    def test_a_plain_new_conversation_is_not_sent_to_compare(self):
        response = self.client.post(reverse("chat:create_conversation"))
        self.assertNotIn("compare", response.url)

    def test_the_conversation_page_opens_the_compare_screen_from_the_flag(self):
        conversation = Conversation.objects.create(user=self.user)
        html = self.client.get(reverse("chat:chat_conversation", args=[conversation.id])).content.decode()
        self.assertIn('params.get("compare") !== "1"', html)
        self.assertIn('id="compareSetupScreen"', html)
        self.assertIn("portalSidebarCompareClick()", html)

    def test_a_quick_command_still_wins_over_the_compare_flag(self):
        response = self.client.post(reverse("chat:create_conversation"), {"intel": "ai", "compare": "1"})
        self.assertIn("intel=ai", response.url)
        self.assertNotIn("compare=1", response.url)
