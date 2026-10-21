"""Stage 4: on schedule, pick the best written stories, build the digest, send it for review,
and post it to the channel once approved.

Picking: stories are ranked by `score`; then an LLM groups the top candidates by real-world event
so each event gets one line, and no person or organisation fills more than `max_per_subject` lines.
If the LLM is off or fails, the plain ranking is used.

Review chat commands: /digest builds a digest now, /status shows pipeline counts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from psycopg.types.json import Jsonb

from ..llm import build_provider
from ..prompts import DigestPlan, curate_system, curate_user
from ..render import format_date, join_text, render_digest, render_line, tidy_line
from ..telegram import TelegramBot, TelegramError
from ..textutil import normalize_title
from .base import DEFAULT, Stage

CANDIDATES_PER_SLOT = 3  # how many ranked candidates per section slot the LLM sees


def score(story: dict, now: datetime) -> float:
    age_h = (now - story["created_at"]).total_seconds() / 3600
    return (
        story["importance"] * 10
        + min(story["source_count"], 6) * 4
        + (10 - min(story["source_priority"], 10))
        - age_h * 0.5
    )


def line_text(story: dict) -> str:
    line = tidy_line(story.get("line") or {})
    return join_text(line.get("lead") or "", line.get("rest") or "") or story["title"]


def apply_plan(
    ranked: dict[str, list[dict]],
    plan: DigestPlan | None,
    max_items: dict[str, int],
    max_per_subject: int,
    scores: dict[int, float],
) -> tuple[dict[str, list[dict]], list[int]]:
    """Picks the digest lines. `ranked` holds each section's candidates, best first.

    Returns the chosen stories per section, and the ids of candidates folded into a chosen line
    (same event), which go out with the digest so they do not come back in the next one.
    """
    stories = {s["id"]: s for lst in ranked.values() for s in lst}
    rank = {sid: i for lst in ranked.values() for i, sid in enumerate(s["id"] for s in lst)}
    events: list[tuple[int, list[int], str | None]] = []  # (kept id, all ids, subject)
    seen: set[int] = set()
    for ev in plan.events if plan else []:
        ids = [i for i in dict.fromkeys(ev.ids) if i in stories and i not in seen]
        if not ids:
            continue
        keep = ev.keep if ev.keep in ids else min(ids, key=rank.get)
        seen.update(ids)
        events.append((keep, ids, normalize_title(ev.subject) or None))
    events += [(sid, [sid], None) for sid in stories if sid not in seen]

    # An event reported by several candidates ranks like a story with more sources.
    def event_score(ev):
        return max(scores[i] for i in ev[1]) + 4 * min(len(ev[1]) - 1, 3)

    chosen: dict[str, list[dict]] = {sec: [] for sec in ranked}
    folded: list[int] = []
    per_subject: dict[str, int] = {}
    for keep, ids, subject in sorted(events, key=event_score, reverse=True):
        sec = stories[keep]["section"]
        if len(chosen[sec]) >= max_items.get(sec, 0):
            continue
        if subject and per_subject.get(subject, 0) >= max_per_subject:
            continue
        if subject:
            per_subject[subject] = per_subject.get(subject, 0) + 1
        chosen[sec].append(stories[keep])
        folded += [i for i in ids if i != keep]
    return chosen, folded


class Publisher(Stage):
    name = "publisher"

    def __init__(self, cfg, db, bot: TelegramBot | None = None, llm=DEFAULT):
        super().__init__(cfg, db)
        if llm is DEFAULT:
            llm = build_provider(cfg.llm) if cfg.llm.enabled and cfg.llm.curate else None
        self.llm = llm
        self.bot = bot or TelegramBot(cfg.channel.bot_token)
        self.tz = ZoneInfo(cfg.channel.timezone)
        self.review_chat = cfg.channel.review_chat_id

    # ---------- building ----------

    async def select_stories(self) -> tuple[dict[str, list[dict]], list[int]]:
        """Returns the chosen stories per section and the ids folded into them (see `apply_plan`)."""
        rows = await self.db.fetch(
            """select * from stories
               where status = 'written' and digest_id is null
                 and created_at > now() - make_interval(hours => %s)""",
            (self.cfg.channel.lookback_hours,),
        )
        now = datetime.now(timezone.utc)
        scores = {r["id"]: score(r, now) for r in rows}
        ranked: dict[str, list[dict]] = {}
        for sec in self.cfg.sections:
            in_sec = sorted((r for r in rows if r["section"] == sec.id), key=lambda r: scores[r["id"]], reverse=True)
            ranked[sec.id] = in_sec[: sec.max_items * CANDIDATES_PER_SLOT]
        plan = await self.plan_events([s for lst in ranked.values() for s in lst])
        return apply_plan(
            ranked, plan, {sec.id: sec.max_items for sec in self.cfg.sections},
            self.cfg.channel.max_per_subject, scores,
        )

    async def plan_events(self, candidates: list[dict]) -> DigestPlan | None:
        if not self.llm or len(candidates) < 2:
            return None
        payload = [{"id": c["id"], "section": c["section"], "text": line_text(c)} for c in candidates]
        try:
            plan = await self.llm.generate(curate_system(self.cfg), curate_user(payload), DigestPlan, max_tokens=4000)
        except Exception as exc:  # the digest must go out even if this step fails
            self.log.warning("grouping digest candidates by event failed, using plain ranking: %s", exc)
            return None
        merged = sum(len(ev.ids) - 1 for ev in plan.events if ev.ids)
        self.log.info("LLM grouped %d candidates into %d events (%d merged)", len(candidates), len(plan.events), merged)
        return plan

    def render(self, by_section: dict[str, list[dict]]) -> list[str]:
        header = self.cfg.channel.header
        if header:
            header = header.format(date=format_date(datetime.now(self.tz).date(), self.cfg.channel.language))
        sections = [
            (sec.title, [render_line(s["line"], s["url"]) for s in by_section.get(sec.id, [])])
            for sec in self.cfg.sections
        ]
        return render_digest(sections, header)

    async def build_digest(self) -> int | None:
        by_section, folded = await self.select_stories()
        story_ids = [s["id"] for lst in by_section.values() for s in lst]
        if not story_ids:
            self.log.info("no stories for a digest")
            if self.review_chat:
                await self.bot.send_message(self.review_chat, "Nu sunt știri noi pentru digest.")
            return None
        parts = self.render(by_section)
        status = "pending_review" if self.review_chat else "approved"
        async with self.db.tx() as conn:
            cur = await conn.execute(
                "insert into digests (status, parts, story_ids) values (%s, %s, %s) returning id",
                (status, Jsonb(parts), story_ids),
            )
            digest_id = (await cur.fetchone())["id"]
            # Folded duplicates belong to the digest too, so they are posted (or freed) with it.
            await conn.execute(
                "update stories set digest_id = %s where id = any(%s)", (digest_id, story_ids + folded)
            )
        self.log.info("digest %d built with %d stories in %d parts", digest_id, len(story_ids), len(parts))

        if self.review_chat:
            await self.send_for_review(digest_id, parts, len(story_ids))
        else:
            await self.post(digest_id)
        return digest_id

    async def send_for_review(self, digest_id: int, parts: list[str], count: int):
        ids = [await self.bot.send_message(self.review_chat, p) for p in parts]
        control = await self.bot.send_message(
            self.review_chat,
            f"Digest #{digest_id}: {count} știri. Public pe canal?",
            buttons=[[
                {"text": "✅ Publică", "callback_data": f"approve:{digest_id}"},
                {"text": "❌ Renunță", "callback_data": f"skip:{digest_id}"},
            ]],
        )
        await self.db.execute(
            "update digests set review_message_ids = %s where id = %s", (Jsonb(ids + [control]), digest_id)
        )

    async def post(self, digest_id: int) -> bool:
        d = await self.db.fetchone("select * from digests where id = %s", (digest_id,))
        if not d or d["status"] not in ("pending_review", "approved"):
            return False
        try:
            ids = [await self.bot.send_message(self.cfg.channel.chat_id, p) for p in d["parts"]]
        except TelegramError as exc:
            await self.db.execute("update digests set status = 'failed', error = %s where id = %s", (str(exc), digest_id))
            self.log.error("posting digest %d failed: %s", digest_id, exc)
            return False
        async with self.db.tx() as conn:
            await conn.execute(
                "update digests set status = 'posted', posted_at = now(), channel_message_ids = %s where id = %s",
                (Jsonb(ids), digest_id),
            )
            await conn.execute("update stories set status = 'posted' where digest_id = %s", (digest_id,))
        self.log.info("digest %d posted", digest_id)
        return True

    async def skip(self, digest_id: int):
        async with self.db.tx() as conn:
            await conn.execute(
                "update digests set status = 'skipped' where id = %s and status = 'pending_review'", (digest_id,)
            )
            # Free the stories so a rebuilt digest (/digest) can use them again.
            await conn.execute("update stories set digest_id = null where digest_id = %s", (digest_id,))

    # ---------- loops ----------

    async def schedule_loop(self):
        while True:
            now = datetime.now(self.tz)
            nxt = min(croniter(expr, now).get_next(datetime) for expr in self.cfg.channel.schedule)
            self.log.info("next digest at %s", nxt.isoformat())
            await asyncio.sleep(max(1.0, (nxt - datetime.now(self.tz)).total_seconds()))
            try:
                await self.build_digest()
            except Exception:
                self.log.exception("building digest failed")

    async def updates_loop(self):
        offset = await self.db.kv_get("telegram_offset")
        while True:
            try:
                updates = await self.bot.get_updates(offset)
            except Exception as exc:
                self.log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(10)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    await self.handle_update(upd)
                except Exception:
                    self.log.exception("handling update failed")
            if updates:
                await self.db.kv_set("telegram_offset", offset)

    async def handle_update(self, upd: dict):
        if "callback_query" in upd:
            cq = upd["callback_query"]
            msg = cq.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(self.review_chat):
                return
            action, _, raw_id = (cq.get("data") or "").partition(":")
            digest_id = int(raw_id) if raw_id.isdigit() else None
            if digest_id is None:
                return
            if action == "approve":
                ok = await self.post(digest_id)
                note = "✅ Publicat pe canal." if ok else "Digestul nu mai poate fi publicat."
            else:
                await self.skip(digest_id)
                note = "❌ Renunțat. Trimite /digest pentru unul nou."
            await self.bot.answer_callback(cq["id"], note)
            await self.bot.edit_message_text(self.review_chat, msg["message_id"], f"Digest #{digest_id}: {note}")
        elif "message" in upd:
            msg = upd["message"]
            if str(msg.get("chat", {}).get("id")) != str(self.review_chat):
                return
            text = (msg.get("text") or "").strip().split("@")[0]
            if text == "/digest":
                await self.build_digest()
            elif text == "/status":
                await self.bot.send_message(self.review_chat, await self.status_text())

    async def status_text(self) -> str:
        items = await self.db.fetch(
            "select status, count(*) n from raw_items where fetched_at > now() - interval '24 hours' group by 1"
        )
        stories = await self.db.fetch(
            "select status, count(*) n from stories where created_at > now() - interval '24 hours' group by 1"
        )
        failing = await self.db.fetch(
            "select source_id, last_error from source_state where consecutive_failures >= 3"
        )
        fmt = lambda rows: ", ".join(f"{r['status']} {r['n']}" for r in rows) or "-"
        lines = [f"Ultimele 24h\nItems: {fmt(items)}\nStories: {fmt(stories)}"]
        if failing:
            lines.append("Surse cu erori: " + ", ".join(r["source_id"] for r in failing))
        return "\n".join(lines)

    async def run_forever(self):
        self.log.info("started")
        tasks = [asyncio.create_task(self.schedule_loop())]
        if self.review_chat:
            tasks.append(asyncio.create_task(self.updates_loop()))
        await asyncio.gather(*tasks)
