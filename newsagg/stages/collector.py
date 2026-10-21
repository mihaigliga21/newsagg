"""Stage 1: fetch every enabled source when it is due and store new items in raw_items."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from ..sources import build_source
from ..sources.base import USER_AGENT
from ..sources.website import WebsiteSource
from .base import Stage


class Collector(Stage):
    name = "collector"

    def __init__(self, cfg, db):
        super().__init__(cfg, db)
        self.http = httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        self.sem = asyncio.Semaphore(6)

    async def run_once(self) -> int:
        states = {r["source_id"]: r for r in await self.db.fetch("select * from source_state")}
        now = datetime.now(timezone.utc)
        due = []
        for src in self.cfg.sources:
            if not src.enabled:
                continue
            st = states.get(src.id)
            last = st and st["last_fetch_at"]
            # Back off a failing source: interval doubles per consecutive failure, up to 8x.
            failures = st["consecutive_failures"] if st else 0
            interval = timedelta(minutes=src.interval_minutes * min(2**failures, 8))
            if not last or now - last >= interval:
                due.append((src, st))
        results = await asyncio.gather(*(self._fetch_one(src, st) for src, st in due))
        new = sum(results)
        if due:
            self.log.info("fetched %d sources, %d new items", len(due), new)
        return 0  # the collector is time driven; never loop immediately

    async def _fetch_one(self, src, state) -> int:
        async with self.sem:
            source = build_source(src, self.http)
            if isinstance(source, WebsiteSource):
                rows = await self.db.fetch(
                    "select external_id from raw_items where source_id = %s order by id desc limit 500", (src.id,)
                )
                source.known_ids = {r["external_id"] for r in rows}
            try:
                result = await source.fetch(
                    etag=state and state["etag"], last_modified=state and state["last_modified"]
                )
            except Exception as exc:
                self.log.warning("source %s failed: %s", src.id, exc)
                await self.db.execute(
                    """insert into source_state (source_id, last_fetch_at, last_error, consecutive_failures)
                       values (%s, now(), %s, 1)
                       on conflict (source_id) do update set last_fetch_at = now(), last_error = excluded.last_error,
                         consecutive_failures = source_state.consecutive_failures + 1""",
                    (src.id, str(exc)[:500]),
                )
                return 0

            max_age = timedelta(hours=self.cfg.filter.max_age_hours)
            now = datetime.now(timezone.utc)
            inserted = 0
            async with self.db.tx() as conn:
                for it in result.items:
                    if it.published_at and now - it.published_at > max_age:
                        continue
                    cur = await conn.execute(
                        """insert into raw_items (source_id, external_id, url, title, summary, published_at)
                           values (%s, %s, %s, %s, %s, %s) on conflict (source_id, external_id) do nothing""",
                        (src.id, it.external_id, it.url, it.title, it.summary, it.published_at),
                    )
                    inserted += cur.rowcount
                await conn.execute(
                    """insert into source_state (source_id, last_fetch_at, last_ok_at, last_error, consecutive_failures, etag, last_modified)
                       values (%s, now(), now(), null, 0, %s, %s)
                       on conflict (source_id) do update set last_fetch_at = now(), last_ok_at = now(), last_error = null,
                         consecutive_failures = 0, etag = excluded.etag, last_modified = excluded.last_modified""",
                    (src.id, result.etag, result.last_modified),
                )
            return inserted

    async def run_forever(self):
        self.log.info("started with %d sources", sum(s.enabled for s in self.cfg.sources))
        while True:
            try:
                await self.run_once()
            except Exception:
                self.log.exception("collect failed")
            await asyncio.sleep(30)
