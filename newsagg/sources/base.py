from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx

from ..config import SourceConfig

USER_AGENT = "Mozilla/5.0 (compatible; newsagg/0.1; +https://t.me/)"


@dataclass
class FetchedItem:
    external_id: str
    url: str
    title: str
    summary: str = ""
    published_at: datetime | None = None


@dataclass
class FetchResult:
    items: list[FetchedItem] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class Source:
    """A source fetches new items. Subclasses implement `fetch`."""

    def __init__(self, cfg: SourceConfig, http: httpx.AsyncClient):
        self.cfg = cfg
        self.http = http

    async def fetch(self, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        raise NotImplementedError
