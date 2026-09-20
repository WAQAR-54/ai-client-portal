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


@override_settings(LIVE_INTELLIGENCE_ENABLED=True)
class QuickStartCardTests(LiveIntelligenceViewBase):
    """Each quick-start card on the chat home switches its own feature on once
    the new conversation opens."""

    def _home(self):
        return self.client.get(reverse("chat:chat_home")).content.decode()

    def test_every_card_says_which_feature_it_starts(self):
        html = self._home()
        for key in ("summarize", "report", "code"):
            self.assertIn(f'name="start" value="{key}"', html)
        self.assertIn('name="compare" value="1"', html)  # Compare has its own flag

    def test_the_report_and_code_cards_carry_a_prompt_that_needs_finishing(self):
        html = self._home()
        self.assertIn('value="Draft a report on"', html)
        self.assertIn('value="Review this code and flag issues:"', html)
        self.assertNotIn("Draft a first pass of this report", html)

    def test_a_card_start_flag_rides_along_with_its_starter_text(self):
        for key in ("summarize", "report", "code"):
            response = self.client.post(
                reverse("chat:create_conversation"), {"starter_text": "Some prompt", "start": key}
            )
            self.assertIn("starter=Some%20prompt", response.url)
            self.assertTrue(response.url.endswith(f"&start={key}"), response.url)

    def test_an_unknown_start_value_is_ignored(self):
        """The value comes from a POST field and is echoed into a URL: only the
        three known keys may pass."""
        for bad in ("evil", "compare", "<script>", "summarize&x=1", ""):
            response = self.client.post(
                reverse("chat:create_conversation"), {"starter_text": "Some prompt", "start": bad}
            )
            self.assertNotIn("start=", response.url, bad)

    def test_a_start_flag_without_a_starter_text_is_ignored(self):
        response = self.client.post(reverse("chat:create_conversation"), {"start": "code"})
        self.assertNotIn("start=", response.url)

    def test_a_quick_command_wins_over_a_start_flag(self):
        response = self.client.post(reverse("chat:create_conversation"), {"intel": "ai", "start": "code"})
        self.assertIn("intel=ai", response.url)
        self.assertNotIn("start=", response.url)

    def test_the_conversation_page_switches_the_matching_feature_on(self):
        conversation = Conversation.objects.create(user=self.user)
        html = self.client.get(reverse("chat:chat_conversation", args=[conversation.id])).content.decode()
        self.assertIn('id="starter-hint"', html)
        self.assertIn('params.get("start")', html)
        self.assertIn('classList.add("attention")', html)  # summarize -> the paperclip
        self.assertIn("portalToggleCodeMode(codeBtn)", html)  # code -> code-focused answers
        for message in ("data-summarize=", "data-report=", "data-report-doc=", "data-code="):
            self.assertIn(message, html)
