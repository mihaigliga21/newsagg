"""Sites without a feed: read a listing page, follow article links, take og:title/og:description."""

from __future__ import annotations

import asyncio
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..textutil import canonical_url, strip_html, truncate, url_hash
from .base import FetchedItem, FetchResult, Source


def extract_links(html: str, base_url: str, selector: str, limit: int) -> list[str]:
    tree = HTMLParser(html)
    seen: list[str] = []
    for node in tree.css(selector):
        href = node.attributes.get("href")
        if not href:
            continue
        url = canonical_url(urljoin(base_url, href))
        if url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen


def extract_article_meta(html: str) -> tuple[str, str]:
    tree = HTMLParser(html)

    def meta(prop: str) -> str:
        node = tree.css_first(f'meta[property="{prop}"]') or tree.css_first(f'meta[name="{prop}"]')
        return (node.attributes.get("content") or "").strip() if node else ""

    title = meta("og:title")
    if not title:
        h1 = tree.css_first("h1")
        title = h1.text(strip=True) if h1 else ""
    return strip_html(title), strip_html(meta("og:description") or meta("description"))


class WebsiteSource(Source):
    def __init__(self, cfg, http):
        super().__init__(cfg, http)
        # Article pages are only fetched for links not seen before; the collector fills this in.
        self.known_ids: set[str] = set()

    async def fetch(self, etag=None, last_modified=None) -> FetchResult:
        resp = await self.http.get(self.cfg.url)
        resp.raise_for_status()
        links = extract_links(resp.text, str(resp.url), self.cfg.link_selector, self.cfg.max_items)
        items: list[FetchedItem] = []
        for link in links:
            ext = url_hash(link)
            if ext in self.known_ids:
                continue
            try:
                page = await self.http.get(link)
                page.raise_for_status()
            except Exception:
                continue
            title, summary = extract_article_meta(page.text)
            if title:
                items.append(FetchedItem(external_id=ext, url=link, title=title, summary=truncate(summary, 1200)))
            await asyncio.sleep(1)  # be polite to small sites
        return FetchResult(items=items)
