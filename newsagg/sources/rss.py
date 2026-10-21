from __future__ import annotations

from datetime import datetime, timezone

import feedparser

from ..textutil import canonical_url, strip_html, truncate, url_hash
from .base import FetchedItem, FetchResult, Source


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def parse_feed(content: bytes, max_items: int = 30) -> list[FetchedItem]:
    feed = feedparser.parse(content)
    items: list[FetchedItem] = []
    for entry in feed.entries[:max_items]:
        link = entry.get("link")
        title = strip_html(entry.get("title"))
        if not link or not title:
            continue
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        items.append(
            FetchedItem(
                external_id=url_hash(link),
                url=canonical_url(link),
                title=title,
                summary=truncate(summary, 1200),
                published_at=_entry_time(entry),
            )
        )
    return items


class RssSource(Source):
    async def fetch(self, etag=None, last_modified=None) -> FetchResult:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        resp = await self.http.get(self.cfg.url, headers=headers)
        if resp.status_code == 304:
            return FetchResult(not_modified=True, etag=etag, last_modified=last_modified)
        resp.raise_for_status()
        return FetchResult(
            items=parse_feed(resp.content, self.cfg.max_items),
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )
