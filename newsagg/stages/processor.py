"""Stage 2: label new raw items (region, topic, importance), drop what is off-scope,
and fold items about the same event into one story."""

from __future__ import annotations

from ..classify import KeywordClassifier
from ..dedupe import find_match
from ..llm import LLMError, build_provider
from ..prompts import ItemLabels, classify_system, classify_user
from ..textutil import normalize_title
from .base import DEFAULT, Stage


class Processor(Stage):
    name = "processor"
    listen = ["newsagg_raw_items"]

    def __init__(self, cfg, db, llm=DEFAULT):
        super().__init__(cfg, db)
        self.keywords = KeywordClassifier(cfg)
        if llm is DEFAULT:
            llm = build_provider(cfg.llm) if cfg.llm.enabled and cfg.llm.classify else None
        self.llm = llm

    async def recover(self):
        n = await self.db.execute(
            "update raw_items set status = 'new', claimed_at = null where status = 'processing'"
        )
        if n:
            self.log.info("released %d items left in processing", n)

    async def _claim(self) -> list[dict]:
        rows = await self.db.fetch(
            """update raw_items set status = 'processing', claimed_at = now()
               where id in (select id from raw_items where status = 'new' order by id
                            limit %s for no key update skip locked)
               returning *""",
            (self.cfg.worker.batch_size,),
        )
        return sorted(rows, key=lambda r: r["id"])  # UPDATE ... RETURNING has no guaranteed order

    async def run_once(self) -> int:
        items = await self._claim()
        if not items:
            return 0

        keep = []
        for it in items:
            if self.keywords.is_blocked(it["title"], it["summary"]):
                await self._finish(it["id"], "rejected", "blocklist")
            else:
                keep.append(it)

        recent = await self._recent_stories()
        labels = await self._label(keep, recent)
        candidates = [(r["id"], r["norm_title"]) for r in recent]
        recent_ids = {r["id"] for r in recent}
        story_of: dict[int, int] = {}  # raw item id -> the story it became or joined
        accepted = dupes = rejected = 0

        for it in keep:
            lab = labels.get(it["id"])
            src = self.cfg.source(it["source_id"])
            if lab is None or not lab["is_news"]:
                await self._finish(it["id"], "rejected", "not news")
                rejected += 1
                continue
            section = self.cfg.section_for_region(lab["region"])
            if section is None:
                await self._finish(it["id"], "rejected", f"region {lab['region']}")
                rejected += 1
                continue

            # The LLM's judgement first, then the title match as a fallback.
            match = (
                await self._same_url_story(it["url"])
                or (lab["same_story"] if lab["same_story"] in recent_ids else None)
                or story_of.get(lab["same_item"])
                or find_match(it["title"], candidates, self.cfg.dedupe.title_similarity, self.cfg.dedupe.min_tokens)
            )
            if match:
                story_of[it["id"]] = match
                await self._merge(match, it, src, lab)
                await self._finish(it["id"], "duplicate", None, match)
                dupes += 1
                continue

            status = "pending_write" if lab["importance"] >= self.cfg.llm.min_importance else "skipped"
            row = await self.db.fetchone(
                """insert into stories (norm_title, title, summary, url, source_id, source_priority,
                                        region, section, topic, importance, status)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                (
                    normalize_title(it["title"]), it["title"], it["summary"], it["url"], it["source_id"],
                    src.priority if src else 5, lab["region"], section, lab["topic"], lab["importance"], status,
                ),
            )
            candidates.append((row["id"], normalize_title(it["title"])))
            story_of[it["id"]] = row["id"]
            await self._finish(it["id"], "accepted", None, row["id"])
            accepted += 1

        self.log.info("processed %d items: %d new stories, %d duplicates, %d rejected",
                      len(items), accepted, dupes, rejected + len(items) - len(keep))
        return len(items)

    async def _label(self, items: list[dict], recent: list[dict] = ()) -> dict[int, dict]:
        out: dict[int, dict] = {}
        if self.llm and items:
            size = self.cfg.llm.batch_size
            for i in range(0, len(items), size):
                chunk = items[i : i + size]
                try:
                    res = await self.llm.generate(
                        classify_system(self.cfg), classify_user(chunk, recent[: self.cfg.dedupe.llm_recent_stories]),
                        ItemLabels, max_tokens=8000
                    )
                    for lab in res.items:
                        topic = lab.topic if lab.topic in self.cfg.topics else "default"
                        out[lab.id] = {
                            "is_news": lab.is_news,
                            "region": lab.region,
                            "topic": topic,
                            "importance": max(1, min(5, lab.importance)),
                            "same_story": lab.same_story,
                            "same_item": lab.same_item if lab.same_item < lab.id else 0,
                        }
                except LLMError as exc:
                    self.log.warning("LLM labelling failed, using keywords for %d items: %s", len(chunk), exc)
        for it in items:
            if it["id"] not in out:
                src = self.cfg.source(it["source_id"])
                out[it["id"]] = {
                    "is_news": True,
                    "region": self.keywords.region(it["title"], it["summary"], src.region if src else None),
                    "topic": self.keywords.topic(it["title"], it["summary"]),
                    "importance": 3,
                    "same_story": 0,
                    "same_item": 0,
                }
        return out

    async def _recent_stories(self) -> list[dict]:
        """Stories of the dedupe window, newest first."""
        return await self.db.fetch(
            """select id, norm_title, title from stories
               where created_at > now() - make_interval(hours => %s) order by id desc""",
            (self.cfg.dedupe.window_hours,),
        )

    async def _same_url_story(self, url: str) -> int | None:
        row = await self.db.fetchone(
            "select id from stories where url = %s and created_at > now() - make_interval(hours => %s) limit 1",
            (url, self.cfg.dedupe.window_hours),
        )
        return row["id"] if row else None

    async def _merge(self, story_id: int, it: dict, src, lab: dict):
        # A better-ranked source takes over the link while the line is not written yet.
        await self.db.execute(
            """update stories set
                 source_count = source_count + 1,
                 importance = greatest(importance, %(imp)s),
                 updated_at = now(),
                 url = case when status = 'pending_write' and %(prio)s < source_priority then %(url)s else url end,
                 source_id = case when status = 'pending_write' and %(prio)s < source_priority then %(src)s else source_id end,
                 source_priority = least(source_priority, %(prio)s),
                 status = case when status = 'skipped' and greatest(importance, %(imp)s) >= %(min)s
                               then 'pending_write' else status end
               where id = %(id)s""",
            {
                "imp": lab["importance"], "prio": src.priority if src else 5, "url": it["url"],
                "src": it["source_id"], "min": self.cfg.llm.min_importance, "id": story_id,
            },
        )

    async def _finish(self, item_id: int, status: str, reason: str | None, story_id: int | None = None):
        await self.db.execute(
            "update raw_items set status = %s, reject_reason = %s, story_id = %s where id = %s",
            (status, reason, story_id, item_id),
        )
