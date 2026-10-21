"""Public Telegram channels through the web preview at https://t.me/s/<channel>. No login needed."""

from __future__ import annotations

import re
from datetime import datetime

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..textutil import canonical_url, first_sentence, truncate
from .base import FetchedItem, FetchResult, Source

_URL_RE = re.compile(r"https?://\S+")


def parse_channel_page(html: str, channel: str, max_items: int = 30) -> list[FetchedItem]:
    tree = HTMLParser(html)
    items: list[FetchedItem] = []
    for msg in tree.css("div.tgme_widget_message"):
        post = msg.attributes.get("data-post")  # "<channel>/<id>"
        text_node = msg.css_first(".tgme_widget_message_text")
        if not post or not text_node:
            continue
        text = text_node.text(separator=" ", strip=True)
        if len(text) < 20:
            continue
        # Prefer the first external article link in the post; fall back to the post itself.
        url = f"https://t.me/{post}"
        for a in text_node.css("a"):
            href = a.attributes.get("href") or ""
            if href.startswith("http") and "t.me/" not in href:
                url = canonical_url(href)
                break
        published = None
        time_node = msg.css_first("time[datetime]")
        if time_node:
            try:
                published = datetime.fromisoformat(time_node.attributes["datetime"])
            except ValueError:
                pass
        items.append(
            FetchedItem(
                external_id=f"tg:{post}",
                url=url,
                title=first_sentence(_URL_RE.sub("", text)),
                summary=truncate(text, 1200),
                published_at=published,
            )
        )
    return items[-max_items:]


class TelegramWebSource(Source):
    async def fetch(self, etag=None, last_modified=None) -> FetchResult:
        channel = self.cfg.channel.lstrip("@")
        resp = await self.http.get(f"https://t.me/s/{channel}")
        resp.raise_for_status()
        return FetchResult(items=parse_channel_page(resp.text, channel, self.cfg.max_items))
