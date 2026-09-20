"""AI replies must not create a heading-level jump under the page's h1."""

import re

from django.template import Context, Template
from django.test import SimpleTestCase

from chat.markdown_utils import normalize_headings, render_markdown


def levels(html):
    return [int(n) for n in re.findall(r"<h([1-6])[ >]", html)]


class NormalizeHeadingsTests(SimpleTestCase):
    def test_a_reply_that_starts_at_h3_is_lifted_to_h2(self):
        html = render_markdown("### Retrieved Information\n\ntext", base_heading_level=2)
        self.assertEqual(levels(html), [2])
        self.assertIn('class="md-h3"', html)  # keeps its look

    def test_relative_structure_is_preserved_not_flattened(self):
        html = render_markdown("# A\n\n## B\n\n### C\n\n## D", base_heading_level=2)
        self.assertEqual(levels(html), [2, 3, 4, 3])

    def test_a_reply_that_starts_at_h1_is_pushed_down_one(self):
        self.assertEqual(levels(render_markdown("# Title\n\nbody", base_heading_level=2)), [2])

    def test_the_level_is_capped_at_h6(self):
        text = "\n\n".join(f"{'#' * n} h{n}" for n in range(1, 7))
        self.assertEqual(levels(render_markdown(text, base_heading_level=2)), [2, 3, 4, 5, 6, 6])  # h7 does not exist

    def test_an_authors_own_gap_does_not_become_a_skipped_level(self):
        self.assertEqual(levels(render_markdown("# A\n\n### C", base_heading_level=2)), [2, 3])
        self.assertEqual(levels(render_markdown("### A\n\n##### C", base_heading_level=2)), [2, 3])

    def test_the_top_heading_is_h2_and_no_step_skips_a_level(self):
        for text in ("### a\n\n#### b", "##### deep", "# a\n\n### c"):
            found = levels(render_markdown(text, base_heading_level=2))
            self.assertEqual(found[0], 2, text)
            for previous, current in zip(found, found[1:]):
                self.assertLessEqual(current - previous, 1, text)

    def test_text_without_headings_is_untouched(self):
        text = "Just a **paragraph** with a list:\n\n- one\n- two\n\n`code`"
        self.assertEqual(render_markdown(text, base_heading_level=2), render_markdown(text))

    def test_default_rendering_keeps_the_authors_levels_for_documents_and_exports(self):
        """chat/document_generation.py and chat/export.py read '# Title' as the title."""
        html = render_markdown("# Title\n\n### Sub")
        self.assertEqual(levels(html), [1, 3])
        self.assertNotIn("md-h", html)

    def test_headings_with_inline_markup_survive(self):
        html = render_markdown("### The `code` **bold** heading", base_heading_level=2)
        self.assertRegex(html, r'<h2 class="md-h3">The <code>code</code> <strong>bold</strong> heading</h2>')

    def test_a_heading_lookalike_inside_a_code_block_is_not_touched(self):
        html = render_markdown("### Real\n\n```\n<h3>not a heading</h3>\n```", base_heading_level=2)
        self.assertEqual(levels(html), [2])
        self.assertIn("&lt;h3&gt;not a heading&lt;/h3&gt;", html)

    def test_sanitising_still_happens_first(self):
        html = render_markdown("### T\n\n<script>alert(1)</script> <h3 onclick=x>y</h3>", base_heading_level=2)
        self.assertNotIn("<script", html)
        self.assertNotIn("onclick", html)

    def test_html_without_headings_passes_through(self):
        self.assertEqual(normalize_headings("<p>x</p>"), "<p>x</p>")


class TemplateFilterTests(SimpleTestCase):
    def test_the_page_filter_normalises_headings(self):
        template = Template("{% load chat_extras %}{{ text|render_markdown }}")
        html = template.render(Context({"text": "### Section\n\nbody"}))
        self.assertEqual(levels(html), [2])
