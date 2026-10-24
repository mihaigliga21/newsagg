"""Builds Telegram HTML for the digest. The renderer owns the markup so model output cannot break it."""

from __future__ import annotations

import html
import re
from datetime import date

from .textutil import strip_diacritics, strip_media_tags

TELEGRAM_LIMIT = 4096

_MONTHS_RO = [
    "ianuarie", "februarie", "martie", "aprilie", "mai", "iunie",
    "iulie", "august", "septembrie", "octombrie", "noiembrie", "decembrie",
]


def format_date(d: date, language: str = "ro") -> str:
    if language == "ro":
        return f"{d.day} {_MONTHS_RO[d.month - 1]} {d.year}"
    return d.isoformat()


# Romanian words a bold lead or a link anchor must not end on (compared without diacritics).
_DANGLING = set(
    "la de din in pe cu pentru despre prin spre catre sub peste dupa pana fara intre printre asupra "
    "a al ai ale un o unei unui unor si sau ori iar dar ca sa ce care lui cel cea cei cele".split()
)
_OPEN_QUOTES = "„«“"
_CLOSE_QUOTES = "”»"
_NO_SPACE_BEFORE = re.compile(r"^[,.;:!?…)\]»”]")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?…])")


def _is_dangling(word: str) -> bool:
    return strip_diacritics(word.lower()).strip(_OPEN_QUOTES + "\"'") in _DANGLING or word in _OPEN_QUOTES


def _unclosed_quote(text: str) -> int:
    """Index of an opening quote that is not closed later in `text`, or -1."""
    i = max(text.rfind(q) for q in _OPEN_QUOTES)
    if i < 0 or any(q in text[i:] for q in _CLOSE_QUOTES):
        return -1
    return i


def join_text(left: str, right: str) -> str:
    """Joins two pieces of a sentence with a space, except before punctuation."""
    if not left or not right:
        return left or right
    return left + right if _NO_SPACE_BEFORE.match(right) else f"{left} {right}"


def tidy_line(line: dict) -> dict:
    """Repairs common LLM slips before rendering: outlet tags (VIDEO, FOTO, LIVE), a bold lead that
    ends on a preposition or inside an open quote, and a link anchor that ends on a preposition."""
    lead, rest, anchor = (
        _SPACE_BEFORE_PUNCT.sub(r"\1", strip_media_tags(line.get(k))) for k in ("lead", "rest", "anchor")
    )

    q = _unclosed_quote(lead)
    if q > 0:
        lead, rest = lead[:q].rstrip(), join_text(lead[q:].strip(), rest)
    words = lead.split()
    moved = []
    while len(words) > 1 and _is_dangling(words[-1]):
        moved.insert(0, words.pop())
    if moved:
        lead, rest = " ".join(words), join_text(" ".join(moved), rest)

    words = anchor.split()
    while len(words) > 1 and _is_dangling(words[-1]):
        words.pop()
    anchor = " ".join(words)
    if anchor[:1] in _OPEN_QUOTES and _unclosed_quote(anchor) == 0:
        anchor = anchor[1:]  # link inside the quote rather than half of it
    return {**line, "lead": lead, "rest": rest, "anchor": anchor}


def template_line(title: str, emoji: str) -> dict:
    """Fallback when no LLM line exists: bold the part before a colon, or the first four words."""
    title = strip_media_tags(title)
    colon = title.find(":")
    if 0 < colon <= 70:
        lead, rest = title[: colon + 1], title[colon + 1 :].strip()
    else:
        words = title.split()
        lead, rest = " ".join(words[:4]), " ".join(words[4:])
    rest_words = rest.split()
    anchor = " ".join(rest_words[-3:]) if rest_words else ""
    return {"emoji": emoji, "lead": lead, "rest": rest, "anchor": anchor}


def render_sources(extra: list[tuple[str, str]] | None) -> str:
    """Links to the other outlets that reported the story, e.g. " (+ <a>TV8</a>, <a>Unimedia</a>)"."""
    if not extra:
        return ""
    links = ", ".join(f'<a href="{html.escape(u, quote=True)}">{html.escape(name)}</a>' for name, u in extra)
    return f" (+ {links})"


def render_line(line: dict, url: str, extra: list[tuple[str, str]] | None = None) -> str:
    return _render_main(line, url) + render_sources(extra)


def _render_main(line: dict, url: str) -> str:
    line = tidy_line(line)
    emoji = html.escape(line.get("emoji") or "📰")
    lead = html.escape((line.get("lead") or "").strip())
    rest = (line.get("rest") or "").strip()
    anchor = (line.get("anchor") or "").strip()
    href = html.escape(url, quote=True)

    if anchor and anchor in rest:
        i = rest.index(anchor)
        rest_html = (
            html.escape(rest[:i])
            + f'<a href="{href}">{html.escape(anchor)}</a>'
            + html.escape(rest[i + len(anchor) :])
        )
    elif rest:
        words = rest.split()
        head, tail = " ".join(words[:-2]), " ".join(words[-2:])
        rest_html = (html.escape(head) + " " if head else "") + f'<a href="{href}">{html.escape(tail)}</a>'
    else:
        return f'{emoji} <a href="{href}"><b>{lead}</b></a>'
    sep = "" if _NO_SPACE_BEFORE.match(rest) else " "
    return f"{emoji} <b>{lead}</b>{sep}{rest_html}"


def render_digest(sections: list[tuple[str | None, list[str]]], header: str | None = None) -> list[str]:
    """sections: [(title or None, [rendered line, ...])]. Returns message parts under Telegram's limit."""
    blocks: list[str] = []
    if header:
        blocks.append(header)
    for title, lines in sections:
        if not lines:
            continue
        if title:
            blocks.append(f"<b>{html.escape(title)}</b>")
        blocks.extend(lines)
    return split_blocks(blocks)


_TAG_RE = re.compile(r"<[^>]+>")


def visible_length(text: str) -> int:
    """Length as Telegram counts it: text after the HTML tags are parsed, in UTF-16 units."""
    return len(html.unescape(_TAG_RE.sub("", text)).encode("utf-16-le")) // 2


def split_blocks(blocks: list[str], limit: int = TELEGRAM_LIMIT, sep: str = "\n\n") -> list[str]:
    parts: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}{sep}{block}" if current else block
        if visible_length(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        current = block[:limit]
    if current:
        parts.append(current)
    return parts
