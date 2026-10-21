"""Stage 4: on schedule, pick the best written stories, build the digest, send it for review,
and post it to the channel once approved.

Review chat commands: /digest builds a digest now, /status shows pipeline counts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from psycopg.types.json import Jsonb

from ..render import format_date, render_digest, render_line
from ..telegram import TelegramBot, TelegramError
from .base import Stage


def score(story: dict, now: datetime) -> float:
    age_h = (now - story["created_at"]).total_seconds() / 3600
    return (
        story["importance"] * 10
        + min(story["source_count"], 6) * 4
        + (10 - min(story["source_priority"], 10))
        - age_h * 0.5
    )


class Publisher(Stage):
    name = "publisher"

    def __init__(self, cfg, db, bot: TelegramBot | None = None):
        super().__init__(cfg, db)
        self.bot = bot or TelegramBot(cfg.channel.bot_token)
        self.tz = ZoneInfo(cfg.channel.timezone)
        self.review_chat = cfg.channel.review_chat_id

    # ---------- building ----------

    async def select_stories(self) -> dict[str, list[dict]]:
        rows = await self.db.fetch(
            """select * from stories
               where status = 'written' and digest_id is null
                 and created_at > now() - make_interval(hours => %s)""",
            (self.cfg.channel.lookback_hours,),
        )
        now = datetime.now(timezone.utc)
        by_section: dict[str, list[dict]] = {}
        for sec in self.cfg.sections:
            chosen = sorted((r for r in rows if r["section"] == sec.id), key=lambda r: score(r, now), reverse=True)
            by_section[sec.id] = chosen[: sec.max_items]
        return by_section

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
        by_section = await self.select_stories()
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
            await conn.execute("update stories set digest_id = %s where id = any(%s)", (digest_id, story_ids))
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
