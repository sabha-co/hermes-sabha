"""
Markdown -> Sabha rich text (ActionText / Trix-compatible HTML) converter.

Sabha stores message bodies as ActionText rich text (`has_rich_text :body`
on `Message`). When the plugin posts to ``POST /api/bots/rooms/:id/messages``,
the server reads the request body as a UTF-8 string and stores it directly
as the rich-text body — no markdown parsing, no rendering step. If the
plugin sends literal markdown (`**bold**`, `- item`, ```` ``` ````), users see
the raw asterisks/dashes/backticks in the UI.

Three deliberate deviations from canonical Trix output:

1. Headings emit ``<h1>``-``<h6>`` even though the Trix editor only supports
   ``<h1>`` natively. Sabha's sanitizer allowlist permits all six levels;
   bot messages are never edited by humans.
2. Paragraph separation uses ``<br><br>`` between blocks, not ``<div>``/``<p>``.
   Visually identical, different in Trix's document model.
3. Pipe tables downgrade to ``<pre>`` wrapping the raw pipe-table text
   (HTML-escaped) — Sabha's sanitizer allowlist excludes table tags so a
   real ``<table>`` would be silently stripped at display time.

Mention handling: Sabha's bot API accepts ``@{user_id}`` literal tokens; the
server's ``format_mentions`` rewrites them at save time. Each ``@{N}`` is
substituted for a NUL-delimited placeholder up front, the pipeline runs,
and the literals are restored at the end. NUL is chosen because it cannot
legally appear in markdown an LLM would produce.
"""

import re

# NUL-delimited placeholder used to protect `@{user_id}` tokens through the
# pipeline. NUL (\x00) cannot legally appear in markdown an LLM would produce.
_MENTION_PLACEHOLDER_PREFIX = "\x00MENTION\x00"
_MENTION_PLACEHOLDER_RE = re.compile("\x00MENTION\x00(\\d+)\x00")
_MENTION_TOKEN_RE = re.compile(r"@\{(\d+)\}")
_FENCED_CODE_RE = re.compile(r"```([\w.+#/-]*)\n([\s\S]*?)```")
_PIPE_TABLE_RE = re.compile(r"(?:^|\n)((?:\|[^\n]+\|(?:\n|$))+)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BLOCKQUOTE_RE = re.compile(r"(?:^|\n)((?:> ?.+(?:\n|$))+)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_BOLD_STARS_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDERS_RE = re.compile(r"__(.+?)__")
_ITALIC_STARS_RE = re.compile(r"(?<![*\w])\*([^*\n]+?)\*(?![*\w])")
_ITALIC_UNDERS_RE = re.compile(r"(?<![_\w])_([^_\n]+?)_(?![_\w])")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:[^()]|\([^)]*\))+)\)")
_UL_RE = re.compile(r"(?:^|\n)((?:[ \t]*[-*+] .+(?:\n|$))+)")
_OL_RE = re.compile(r"(?:^|\n)((?:[ \t]*\d+\. .+(?:\n|$))+)")
_HR_RE = re.compile(r"^[-*_]{3,}$", re.MULTILINE)
_TRIPLE_NEWLINE_RE = re.compile(r"\n{3,}")
_BLOCK_PREFIX_RE = re.compile(r"^<(pre|ul|ol|blockquote|h[1-6]|hr)", re.IGNORECASE)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _sub_pipe_tables(html: str) -> str:
    """Downgrade pipe tables to <pre> blocks, but only outside existing <pre>."""

    pre_ranges: list[tuple[int, int]] = []
    for m in re.finditer(r"<pre[^>]*>", html):
        end = html.find("</pre>", m.end())
        if end != -1:
            pre_ranges.append((m.start(), end + len("</pre>")))

    def in_pre(start: int, end: int) -> bool:
        return any(p_start <= start and end <= p_end for p_start, p_end in pre_ranges)

    def repl(match: re.Match) -> str:
        block = match.group(1)
        if in_pre(match.start(), match.end()):
            return match.group(0)
        rows = block.strip().split("\n")
        if len(rows) < 2:
            return match.group(0)
        if not re.fullmatch(r"[\s|:\-]+", rows[1]):
            return match.group(0)
        return "\n<pre>" + _escape_html(block.rstrip()) + "</pre>\n"

    return _PIPE_TABLE_RE.sub(repl, html)


def markdown_to_sabha_rich_text(md: str) -> str:
    """Convert markdown to Sabha-compatible Trix/ActionText HTML.

    Handles: headings, bold, italic, strikethrough, inline code, fenced code
    blocks, links, blockquotes, unordered/ordered lists, pipe tables (as
    ``<pre>`` fallback), horizontal rules, and paragraph/line breaks.
    Preserves ``@{user_id}`` mention tokens verbatim so the server's
    ``format_mentions`` rewrite runs against them at save time.
    """
    if not md:
        return ""

    mentions: list[str] = []

    def stash_mention(m: re.Match) -> str:
        mentions.append(m.group(0))
        return _MENTION_PLACEHOLDER_PREFIX + str(len(mentions) - 1) + "\x00"

    html = _MENTION_TOKEN_RE.sub(stash_mention, md)

    def fence_repl(m: re.Match) -> str:
        lang, code = m.group(1), m.group(2)
        lang_attr = f' class="language-{lang}"' if lang else ""
        return f"<pre{lang_attr}>{_escape_html(code.rstrip(chr(10)))}</pre>"

    html = _FENCED_CODE_RE.sub(fence_repl, html)
    html = _sub_pipe_tables(html)
    html = _INLINE_CODE_RE.sub(lambda m: f"<code>{_escape_html(m.group(1))}</code>", html)

    def blockquote_repl(m: re.Match) -> str:
        block = m.group(1)
        inner = "\n".join(
            re.sub(r"^> ?", "", line) for line in block.split("\n")
        ).strip()
        return f"\n<blockquote>{inner}</blockquote>\n"

    html = _BLOCKQUOTE_RE.sub(blockquote_repl, html)
    html = _HEADING_RE.sub(
        lambda m: f"<h{len(m.group(1))}>{m.group(2).strip()}</h{len(m.group(1))}>",
        html,
    )
    html = _BOLD_STARS_RE.sub(r"<strong>\1</strong>", html)
    html = _BOLD_UNDERS_RE.sub(r"<strong>\1</strong>", html)
    html = _ITALIC_STARS_RE.sub(r"<em>\1</em>", html)
    html = _ITALIC_UNDERS_RE.sub(r"<em>\1</em>", html)
    html = _STRIKE_RE.sub(r"<del>\1</del>", html)
    html = _LINK_RE.sub(r'<a href="\2">\1</a>', html)

    def list_repl(prefix_re: str, tag: str):
        item_strip = re.compile(prefix_re)

        def repl(m: re.Match) -> str:
            block = m.group(1).strip()
            items = "\n".join(
                f"<li>{item_strip.sub('', line).strip()}</li>"
                for line in block.split("\n")
            )
            return f"\n<{tag}>\n{items}\n</{tag}>\n"

        return repl

    html = _UL_RE.sub(list_repl(r"^[ \t]*[-*+] ", "ul"), html)
    html = _OL_RE.sub(list_repl(r"^[ \t]*\d+\. ", "ol"), html)
    html = _HR_RE.sub("<hr>", html)
    html = _TRIPLE_NEWLINE_RE.sub("\n\n", html)

    paragraphs = []
    for para in html.split("\n\n"):
        trimmed = para.strip()
        if not trimmed:
            continue
        if _BLOCK_PREFIX_RE.match(trimmed):
            paragraphs.append(trimmed)
        else:
            paragraphs.append(trimmed.replace("\n", "<br>\n"))
    html = "<br>\n<br>\n".join(paragraphs)

    def restore(m: re.Match) -> str:
        idx = int(m.group(1))
        return mentions[idx] if idx < len(mentions) else m.group(0)

    html = _MENTION_PLACEHOLDER_RE.sub(restore, html)
    return html.strip()
