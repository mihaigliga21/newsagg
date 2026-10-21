"""Builds Telegram HTML for the digest. The renderer owns the markup so model output cannot break it."""

from __future__ import annotations

import html
from datetime import date

TELEGRAM_LIMIT = 4096

_MONTHS_RO = [
    "ianuarie", "februarie", "martie", "aprilie", "mai", "iunie",
    "iulie", "august", "septembrie", "octombrie", "noiembrie", "decembrie",
]


def format_date(d: date, language: str = "ro") -> str:
    if language == "ro":
        return f"{d.day} {_MONTHS_RO[d.month - 1]} {d.year}"
    return d.isoformat()


def template_line(title: str, emoji: str) -> dict:
    """Fallback when no LLM line exists: bold the part before a colon, or the first four words."""
    title = title.strip()
    colon = title.find(":")
    if 0 < colon <= 70:
        lead, rest = title[: colon + 1], title[colon + 1 :].strip()
    else:
        words = title.split()
        lead, rest = " ".join(words[:4]), " ".join(words[4:])
    rest_words = rest.split()
    anchor = " ".join(rest_words[-3:]) if rest_words else ""
    return {"emoji": emoji, "lead": lead, "rest": rest, "anchor": anchor}


def render_line(line: dict, url: str) -> str:
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
    return f"{emoji} <b>{lead}</b> {rest_html}"


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


def split_blocks(blocks: list[str], limit: int = TELEGRAM_LIMIT, sep: str = "\n\n") -> list[str]:
    parts: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}{sep}{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        current = block[:limit]
    if current:
        parts.append(current)
    return parts
