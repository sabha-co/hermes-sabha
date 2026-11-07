"""Markdown from the agent → the HTML body Sabha's apps API takes.

Sabha stores rich text, not Markdown: a ``text/plain`` post shows its asterisks. So the agent's
Markdown is rendered here, with raw HTML in it escaped rather than passed through. Sabha sanitizes
the body again on its side and keeps no tables, so tables stay as their source text.

``@{user_id}`` survives rendering untouched; Sabha turns it into a real mention when the user is a
member of the room.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_TASK = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[([ xX])\]\s", re.MULTILINE)

_renderer = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("strikethrough")


def to_html(markdown: str) -> str:
    """Render agent Markdown as a Sabha message body. Images become links: Sabha shows an image
    only from a file attached to the message. Task-list boxes become ☐/☑, since Sabha keeps no
    checkbox inputs."""
    source = _IMAGE.sub(lambda m: f"[{m.group(1) or m.group(2)}]({m.group(2)})", markdown)
    source = _TASK.sub(lambda m: f"{m.group(1)}{'☐' if m.group(2) == ' ' else '☑'} ", source)
    return _renderer.render(source).strip()


def strip_mention(text: str, name: str) -> str:
    """Drop ``@name`` (the app's own mention) so the agent sees the question, not its address."""
    if not name:
        return text.strip()
    cleaned = re.sub(rf"(?<!\w)@{re.escape(name)}(?!\w)", "", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()
