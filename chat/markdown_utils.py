"""Server-side Markdown rendering for assistant replies.

Rendered server-side (not client-side JS) so a prompt-injected reply can't
smuggle live HTML/script past us — bleach strips anything outside this
allow-list regardless of what the model actually returned.
"""

import re

import bleach
import markdown as _markdown

ALLOWED_TAGS = [
    "p",
    "br",
    "hr",
    "strong",
    "em",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
]
ALLOWED_ATTRS = {
    "a": ["href", "title"],
    # fenced_code puts the fence's language tag here as class="language-xxx"
    # - kept so the client can syntax-highlight it (see base.html's highlight.js
    # wiring); bleach's own class-name handling here is a plain allow-list of
    # the ATTRIBUTE, not of arbitrary values, so this can't be used to smuggle
    # anything beyond a CSS class string onto the element.
    "code": ["class"],
}


_HEADING = re.compile(r"<h([1-6])>(.*?)</h\1>", re.DOTALL)

# Headings inside a reply on the chat page sit under the page's own h1, so the
# top level a reply may use is h2 (see normalize_headings).
PAGE_BASE_HEADING_LEVEL = 2


def normalize_headings(html: str, base_level: int = PAGE_BASE_HEADING_LEVEL) -> str:
    """Re-level the headings in already-sanitised HTML so the highest one becomes
    `base_level`, keeping every relative step (# / ## / ### -> h2 / h3 / h4) and
    capping at h6. Models freely write "###" as their first heading; on the page
    that put an h3 straight under the h1 and skipped h2 (an accessibility
    heading-order violation, and a confusing outline for screen-reader users).

    The original level is kept as class="md-hN", which the stylesheet uses to keep
    the heading looking exactly as before - only its semantic level changes."""
    levels = sorted({int(m.group(1)) for m in _HEADING.finditer(html)})
    if not levels:
        return html
    # Rank the levels the reply actually uses and hand out consecutive levels from
    # the base: order and nesting are kept, and an author's own gap (a "#" followed
    # by a "###") does not become a skipped level either.
    new_level = {original: min(6, base_level + rank) for rank, original in enumerate(levels)}

    def relevel(match):
        original = int(match.group(1))
        new = new_level[original]
        return f'<h{new} class="md-h{original}">{match.group(2)}</h{new}>'

    return _HEADING.sub(relevel, html)


def render_markdown(text: str, base_heading_level: int | None = None) -> str:
    """Markdown -> sanitised HTML. Pass `base_heading_level` only for on-page display;
    documents and exports (chat/document_generation.py, chat/export.py) keep the
    author's own levels, since they read `# Title` as the document title."""
    if not text:
        return ""
    html = _markdown.markdown(text, extensions=["fenced_code", "tables", "nl2br"])
    html = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
    if base_heading_level is not None:
        html = normalize_headings(html, base_heading_level)
    return html
