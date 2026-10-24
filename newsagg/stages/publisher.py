"""Stage 4: on schedule, pick the best written stories, build the digest, send it for review,
and post it to the channel once approved.

Picking: stories are ranked by `score`; then an LLM groups the top candidates by real-world event
so each event gets one line, and no person or organisation fills more than `max_per_subject` lines.
The line links its own outlet; the other outlets that reported the event are linked after it.
If the LLM is off or fails, the plain ranking is used.

Missed slots: the last schedule slot that got a digest is kept in kv (`digest_last_slot`). A slot
that failed, or passed while the publisher was down, is caught up at the next start (or by the next
slot). Several missed slots from the same day become one digest; slots from earlier days are dropped.

Review chat commands: /digest builds a digest now, /status shows pipeline counts.
"""

from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from psycopg.types.json import Jsonb

from ..llm import build_provider
from ..prompts import DigestPlan, curate_system, curate_user
from ..dedupe import similarity
from ..render import format_date, join_text, render_digest, render_line, tidy_line
from ..telegram import TelegramBot, TelegramError
from ..textutil import normalize_title
from .base import DEFAULT, Stage

CANDIDATES_PER_SLOT = 3  # how many ranked candidates per section slot the LLM sees
RETRY_DELAYS = (30, 120, 300, 600)  # seconds before each new try of a scheduled digest that failed
CATCH_UP_GAP = timedelta(minutes=30)  # at start, don't catch up if the next slot is this close
LAST_SLOT_KEY = "digest_last_slot"
CURATE_MAX_TOKENS = 16000  # the plan is short, but some models spend output tokens on reasoning first


def score(story: dict, now: datetime) -> float:
    age_h = (now - story["created_at"]).total_seconds() / 3600
    return (
        story["importance"] * 10
        + min(story["source_count"], 6) * 4
        + (10 - min(story["source_priority"], 10))
        - age_h * 0.5
    )


def _grouping_failed(exc: Exception, section: str | None = None) -> str:
    where = f" pentru secțiunea {section}" if section else ""
    return (f"⚠️ Gruparea pe evenimente a eșuat{where} ({html.escape(str(exc)[:200])}); "
            "am folosit doar ordinea și potrivirea după titlu.")


def line_text(story: dict) -> str:
    line = tidy_line(story.get("line") or {})
    return join_text(line.get("lead") or "", line.get("rest") or "") or story["title"]


@dataclass
class Selection:
    by_section: dict[str, list[dict]]
    folded: list[int] = field(default_factory=list)  # same event as a chosen line, or already sent
    event_ids: dict[int, list[int]] = field(default_factory=dict)  # chosen story id -> all story ids of its event
    repeats: int = 0  # events left out because a recent digest already had them
    notes: list[str] = field(default_factory=list)  # shown in the review chat


@dataclass
class _Event:
    keep: int
    ids: list[int]
    subject: str | None = None
    section: str | None = None
    repeat: bool = False


def apply_plan(
    cfg,
    ranked: dict[str, list[dict]],
    plan: DigestPlan | None,
    scores: dict[int, float],
    sent_titles: list[str] = (),
) -> Selection:
    """Picks the digest lines. `ranked` holds each section's candidates, best first; `sent_titles`
    are the normalized titles of stories in recent digests.

    One line per event: the LLM plan groups candidates, then candidates with very similar titles
    are merged as a safety net (this also runs when there is no plan). Events a recent digest
    already had are left out rather than replaced by weaker news, so a quiet day gives a short digest.
    """
    stories = {s["id"]: s for lst in ranked.values() for s in lst}
    rank = {sid: i for lst in ranked.values() for i, sid in enumerate(s["id"] for s in lst)}
    events: list[_Event] = []
    seen: set[int] = set()
    for ev in plan.events if plan else []:
        ids = [i for i in dict.fromkeys(ev.ids) if i in stories and i not in seen]
        if not ids:
            continue
        keep = ev.keep if ev.keep in ids else min(ids, key=rank.get)
        seen.update(ids)
        section = cfg.section_for_region(ev.region)
        events.append(_Event(keep, ids, normalize_title(ev.subject) or None,
                             section if section in ranked else None, ev.already_sent))
    events += [_Event(sid, [sid]) for sid in stories if sid not in seen]

    # An event reported by several candidates ranks like a story with more sources.
    def event_score(ev: _Event):
        return max(scores[i] for i in ev.ids) + 4 * min(len(ev.ids) - 1, 3)

    threshold = cfg.dedupe.digest_similarity
    merged: list[_Event] = []
    for ev in sorted(events, key=event_score, reverse=True):
        title = stories[ev.keep]["norm_title"]
        into = next((m for m in merged if similarity(title, stories[m.keep]["norm_title"]) >= threshold), None)
        if into:
            into.ids += ev.ids
            into.repeat = into.repeat or ev.repeat
        else:
            merged.append(ev)
    for ev in merged:
        if not ev.repeat:
            ev.repeat = any(
                similarity(stories[i]["norm_title"], t) >= threshold for i in ev.ids for t in sent_titles
            )

    max_items = {sec.id: sec.max_items for sec in cfg.sections}
    sel = Selection({sec: [] for sec in ranked})
    per_subject: dict[str, int] = {}
    for ev in sorted(merged, key=event_score, reverse=True):
        if ev.repeat:
            sel.repeats += 1
            sel.folded += ev.ids
            continue
        sec = ev.section or stories[ev.keep]["section"]
        if len(sel.by_section[sec]) >= max_items.get(sec, 0):
            continue
        if ev.subject and per_subject.get(ev.subject, 0) >= cfg.channel.max_per_subject:
            continue
        if ev.subject:
            per_subject[ev.subject] = per_subject.get(ev.subject, 0) + 1
        sel.by_section[sec].append(stories[ev.keep])
        sel.event_ids[ev.keep] = list(dict.fromkeys([ev.keep, *ev.ids]))
        sel.folded += [i for i in ev.ids if i != ev.keep]
    return sel


class Publisher(Stage):
    name = "publisher"
    retry_delays = RETRY_DELAYS

    def __init__(self, cfg, db, bot: TelegramBot | None = None, llm=DEFAULT):
        super().__init__(cfg, db)
        if llm is DEFAULT:
            llm = build_provider(cfg.llm) if cfg.llm.enabled and cfg.llm.curate else None
        self.llm = llm
        self.bot = bot or TelegramBot(cfg.channel.bot_token)
        self.tz = ZoneInfo(cfg.channel.timezone)
        self.review_chat = cfg.channel.review_chat_id
        self.built_id: int | None = None  # set as soon as build_digest has saved a digest

    # ---------- building ----------

    async def select_stories(self) -> Selection:
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
        sent = await self.recently_sent()
        plan, notes = await self.plan_events([s for lst in ranked.values() for s in lst], sent)
        sel = apply_plan(self.cfg, ranked, plan, scores, [s["norm_title"] for s in sent])
        sel.notes = notes
        if sel.repeats:
            self.log.info("left out %d events already in recent digests", sel.repeats)
        return sel

    async def recently_sent(self) -> list[dict]:
        """Stories in digests posted or waiting for review within `repeat_window_hours`."""
        return await self.db.fetch(
            """select s.id, s.title, s.norm_title, s.line from stories s join digests d on d.id = s.digest_id
               where d.status in ('pending_review', 'approved', 'posted')
                 and d.created_at > now() - make_interval(hours => %s)
               order by d.created_at desc""",
            (self.cfg.channel.repeat_window_hours,),
        )

    async def plan_events(self, candidates: list[dict], sent: list[dict]) -> tuple[DigestPlan | None, list[str]]:
        """Groups the candidates by event in one LLM call. If that fails (a reply too long for
        max_tokens, a broken reply), each section is grouped on its own: smaller calls, at the
        cost of not seeing events reported in two sections."""
        if not self.llm or not candidates:
            return None, []
        sent_lines = list(dict.fromkeys(line_text(s) for s in sent))[:80]
        try:
            plan = await self._plan(candidates, sent_lines)
        except Exception as exc:  # the digest must go out even if this step fails
            by_section: dict[str, list[dict]] = {}
            for c in candidates:
                by_section.setdefault(c["section"], []).append(c)
            if len(by_section) < 2:
                return None, [_grouping_failed(exc)]
            self.log.warning("grouping digest candidates by event failed (%s); grouping each section alone", exc)
            plan, failed = DigestPlan(events=[]), []
            for sec, cands in by_section.items():
                try:
                    plan.events += (await self._plan(cands, sent_lines)).events
                except Exception as sec_exc:
                    self.log.warning("grouping section %s by event failed: %s", sec, sec_exc)
                    failed.append((sec, sec_exc))
            if len(failed) == len(by_section):
                return None, [_grouping_failed(failed[0][1])]
            notes = [_grouping_failed(e, sec) for sec, e in failed]
        else:
            notes = []
        merged = sum(len(ev.ids) - 1 for ev in plan.events if ev.ids)
        repeats = sum(ev.already_sent for ev in plan.events)
        self.log.info("LLM grouped %d candidates into %d events (%d merged, %d already sent)",
                      len(candidates), len(plan.events), merged, repeats)
        return plan, notes

    async def _plan(self, candidates: list[dict], sent_lines: list[str]) -> DigestPlan:
        payload = [{"id": c["id"], "section": c["section"], "title": c["title"], "text": line_text(c)}
                   for c in candidates]
        return await self.llm.generate(
            curate_system(self.cfg), curate_user(payload, sent_lines), DigestPlan, max_tokens=CURATE_MAX_TOKENS
        )

    async def more_sources(self, sel: Selection) -> dict[int, list[tuple[str, str]]]:
        """For each chosen story, (outlet name, url) of the other outlets that reported its event:
        items merged into its story or into the stories folded into it. Best-ranked outlets first,
        one link per outlet, at most `channel.max_extra_links`."""
        limit = self.cfg.channel.max_extra_links
        if not limit or not sel.event_ids:
            return {}
        all_ids = [i for ids in sel.event_ids.values() for i in ids]
        rows = await self.db.fetch(
            """select story_id, source_id, url from raw_items
               where story_id = any(%s) and status in ('accepted', 'duplicate')
               union all
               select id, source_id, url from stories where id = any(%s)""",
            (all_ids, all_ids),
        )
        by_story: dict[int, list[dict]] = {}
        for r in rows:
            by_story.setdefault(r["story_id"], []).append(r)
        chosen = {s["id"]: s for lst in sel.by_section.values() for s in lst}

        def priority(r):
            src = self.cfg.source(r["source_id"])
            return src.priority if src else 5

        out: dict[int, list[tuple[str, str]]] = {}
        for keep, ids in sel.event_ids.items():
            found = sorted((r for i in ids for r in by_story.get(i, [])), key=priority)
            seen, links = {chosen[keep]["source_id"]}, []
            for r in found:
                if r["source_id"] in seen or r["url"] == chosen[keep]["url"]:
                    continue
                seen.add(r["source_id"])
                src = self.cfg.source(r["source_id"])
                links.append((src.display_name if src else r["source_id"], r["url"]))
            if links:
                out[keep] = links[:limit]
        return out

    def render(self, by_section: dict[str, list[dict]], extra: dict[int, list[tuple[str, str]]] | None = None) -> list[str]:
        header = self.cfg.channel.header
        if header:
            header = header.format(date=format_date(datetime.now(self.tz).date(), self.cfg.channel.language))
        extra = extra or {}
        sections = [
            (sec.title, [render_line(s["line"], s["url"], extra.get(s["id"])) for s in by_section.get(sec.id, [])])
            for sec in self.cfg.sections
        ]
        return render_digest(sections, header)

    async def build_digest(self, extra_notes: list[str] = ()) -> int | None:
        sel = await self.select_stories()
        story_ids = [s["id"] for lst in sel.by_section.values() for s in lst]
        notes = [*extra_notes, *sel.notes] + ([f"Am omis {sel.repeats} știri deja trimise în ultimele "
                              f"{self.cfg.channel.repeat_window_hours} de ore."] if sel.repeats else [])
        if not story_ids:
            self.log.info("no stories for a digest")
            if self.review_chat:
                await self.bot.send_message(self.review_chat, "\n".join(["Nu sunt știri noi pentru digest."] + notes))
            return None
        parts = self.render(sel.by_section, await self.more_sources(sel))
        folded = sel.folded
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
        self.built_id = digest_id
        self.log.info("digest %d built with %d stories in %d parts", digest_id, len(story_ids), len(parts))

        if self.review_chat:
            await self.send_for_review(digest_id, parts, len(story_ids), notes)
        else:
            await self.post(digest_id)
        return digest_id

    async def send_for_review(self, digest_id: int, parts: list[str], count: int, notes: list[str] = ()):
        ids = [await self.bot.send_message(self.review_chat, p) for p in parts]
        control = await self.bot.send_message(
            self.review_chat,
            "\n".join([*notes, f"Digest #{digest_id}: {count} știri. Public pe canal?"]),
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

    # ---------- schedule ----------

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def next_slot(self, after: datetime) -> datetime:
        return min(croniter(expr, after).get_next(datetime) for expr in self.cfg.channel.schedule)

    def slots_between(self, start: datetime, end: datetime) -> list[datetime]:
        """Schedule slots in (start, end], oldest first."""
        slots, t = [], start
        while (t := self.next_slot(t)) <= end:
            slots.append(t)
        return slots

    async def last_slot(self) -> datetime | None:
        """The last slot that got a digest. Before the first slot is recorded, the slot before the
        newest digest stands in for it; None if there has never been a digest."""
        raw = await self.db.kv_get(LAST_SLOT_KEY)
        if raw:
            return datetime.fromisoformat(raw).astimezone(self.tz)
        row = await self.db.fetchone("select max(created_at) t from digests")
        if not row or not row["t"]:
            return None
        newest = row["t"].astimezone(self.tz)
        return max(croniter(expr, newest).get_prev(datetime) for expr in self.cfg.channel.schedule)

    async def mark_slot_done(self, slot: datetime):
        try:
            last = await self.last_slot()
            if not last or slot > last:
                await self.db.kv_set(LAST_SLOT_KEY, slot.isoformat())
        except Exception as exc:  # worst case the slot is caught up again at the next start
            self.log.warning("could not record digest slot %s: %s", slot.isoformat(), exc)

    async def missed_slots(self, before: datetime) -> list[datetime]:
        """Slots after the last one that got a digest and before `before`, from `before`'s day only."""
        last = await self.last_slot()
        if last is None:
            return []
        return [t for t in self.slots_between(last, before - timedelta(seconds=1)) if t.date() == before.date()]

    async def catch_up(self):
        """At start: build one digest for the slots missed today, unless the next slot is close
        (it will include the same stories)."""
        now = self.now()
        if await self.last_slot() is None:  # first run: nothing to catch up
            if prev := self.slots_between(now - timedelta(days=1), now):
                await self.mark_slot_done(prev[-1])
            return
        missed = await self.missed_slots(now + timedelta(seconds=1))
        if not missed:
            return
        if self.next_slot(now) - now < CATCH_UP_GAP:
            self.log.info("missed digest slots %s; the next slot covers them", _times(missed))
            return
        self.log.warning("catching up missed digest slots %s", _times(missed))
        await self.scheduled_digest(missed[-1], late=True)

    async def schedule_loop(self):
        try:
            await self.catch_up()
        except Exception:
            self.log.exception("catching up missed digests failed")
        while True:
            nxt = self.next_slot(self.now())
            self.log.info("next digest at %s", nxt.isoformat())
            await asyncio.sleep(max(1.0, (nxt - self.now()).total_seconds()))
            await self.scheduled_digest(nxt)

    async def scheduled_digest(self, slot: datetime, late: bool = False):
        """Builds the digest for `slot`, trying again a few times if it fails before a digest is
        saved (database or Telegram briefly unreachable), so a slot is not lost to a short outage.
        Earlier slots of the same day that failed are covered by this digest too; `late` marks a
        digest built after its slot (catching up at start)."""
        for attempt, delay in enumerate((*self.retry_delays, None), 1):
            self.built_id = None
            try:
                missed = await self.missed_slots(slot) + ([slot] if late else [])
                await self.build_digest([_missed_note(missed)] if missed else [])
            except Exception as exc:
                if self.built_id is None and delay is not None:
                    self.log.warning("building digest failed (%s: %s); retrying in %ds",
                                     type(exc).__name__, exc, delay)
                    await asyncio.sleep(delay)
                    continue
                if self.built_id is None:  # the slot stays missed: the next start or slot catches it up
                    self.log.exception("building digest failed after %d attempts", attempt)
                    await self.report_failure(exc)
                    return
                # Saved; retrying would build a second digest.
                self.log.exception("digest %d was saved but sending it failed", self.built_id)
            await self.mark_slot_done(slot)
            return

    async def report_failure(self, exc: Exception):
        if not self.review_chat:
            return
        try:
            await self.bot.send_message(
                self.review_chat,
                f"⚠️ Digestul programat nu a putut fi construit ({html.escape(str(exc)[:200])}). "
                "Va fi recuperat la următoarea pornire sau inclus în următorul digest; "
                "trimite /digest pentru a încerca acum.",
            )
        except Exception as send_exc:
            self.log.warning("could not report the failure to the review chat: %s", send_exc)

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
                # A digest built by hand also covers the slots that failed before it.
                now = self.now()
                if passed := self.slots_between(now - timedelta(days=1), now):
                    await self.mark_slot_done(passed[-1])
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


def _times(slots: list[datetime]) -> str:
    return ", ".join(t.strftime("%H:%M") for t in slots)


def _missed_note(slots: list[datetime]) -> str:
    if len(slots) == 1:
        return f"Include și digestul ratat de la {_times(slots)}."
    return f"Include și digestele ratate de la {_times(slots)}."
