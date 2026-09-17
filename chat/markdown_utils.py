"""Server-side Markdown rendering for assistant replies.

Rendered server-side (not client-side JS) so a prompt-injected reply can't
smuggle live HTML/script past us — bleach strips anything outside this
allow-list regardless of what the model actually returned.
"""

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


def render_markdown(text: str) -> str:
    if not text:
        return ""
    html = _markdown.markdown(text, extensions=["fenced_code", "tables", "nl2br"])
    return bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
