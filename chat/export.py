"""Personal conversation export (PDF / Markdown / plain text) - always the
requesting user's own conversation. Admin-side bulk usage export is a
separate feature (governance app); this one is scoped per-user by the view
that calls it (see chat/views.py's _owned_conversation_or_404).
"""

import re
from io import BytesIO

from django.utils import timezone
from django.utils.html import escape
from xhtml2pdf import pisa

from chat.markdown_utils import render_markdown
from chat.models import Message


def _sender_label(message):
    return "You" if message.role == Message.Role.USER else "Assistant"


def render_conversation_markdown(conversation):
    lines = [f"# {conversation.title}", "", f"_Exported {timezone.now():%Y-%m-%d %H:%M}_", ""]
    for message in conversation.messages.all():
        lines.append(f"**{_sender_label(message)}** — {message.created_at:%Y-%m-%d %H:%M}")
        lines.append("")
        lines.append(message.content or "*(no content)*")
        lines.append("")
    return "\n".join(lines)


def render_conversation_text(conversation):
    lines = [conversation.title, f"Exported {timezone.now():%Y-%m-%d %H:%M}", "=" * 40, ""]
    for message in conversation.messages.all():
        lines.append(f"{_sender_label(message)} ({message.created_at:%Y-%m-%d %H:%M}):")
        content = message.content or "(no content)"
        # Light markdown-syntax stripping so this reads as plain prose, not
        # raw source - full fidelity isn't the point of this format, that's
        # what the Markdown/PDF exports are for.
        content = re.sub(r"[*_`#]+", "", content)
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


def render_conversation_pdf(conversation) -> bytes:
    html_parts = [
        "<html><head><style>",
        "body { font-family: Helvetica, sans-serif; font-size: 11pt; }",
        "h1 { font-size: 16pt; }",
        ".meta { color: #666; font-size: 9pt; margin-bottom: 16px; }",
        ".msg { margin-bottom: 14px; }",
        ".sender { font-weight: bold; }",
        ".time { color: #888; font-size: 8pt; }",
        "pre, code { background: #f2f2f2; padding: 2px 4px; }",
        "</style></head><body>",
        f"<h1>{escape(conversation.title)}</h1>",
        f"<div class='meta'>Exported {timezone.now():%Y-%m-%d %H:%M}</div>",
    ]
    for message in conversation.messages.all():
        html_parts.append("<div class='msg'>")
        html_parts.append(
            f"<div class='sender'>{escape(_sender_label(message))} "
            f"<span class='time'>{message.created_at:%Y-%m-%d %H:%M}</span></div>"
        )
        html_parts.append(render_markdown(message.content) or "<p><em>(no content)</em></p>")
        html_parts.append("</div>")
    html_parts.append("</body></html>")
    html = "".join(html_parts)

    buffer = BytesIO()
    status = pisa.CreatePDF(html, dest=buffer)
    if status.err:
        raise ValueError(f"PDF generation failed for conversation {conversation.id}")
    return buffer.getvalue()
