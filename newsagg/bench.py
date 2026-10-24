"""`newsagg llm-bench`: run the three LLM tasks on recent real data with one or more models and
write a Markdown report, to judge a model (a local Ollama one, say) before switching to it.

Read-only: nothing is written to the database and nothing is sent to Telegram.

- labelling: the newest processed items, labelled again; compared with the stored labels (region,
  topic, importance) and with the stored merge decision (did the item join an older story?)
- writing: the newest written stories, written again; shown next to the stored line
- grouping: the candidates of a digest built now (sent or not), grouped by event
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .llm import LLMError, build_provider
from .prompts import DigestLines, ItemLabels, classify_system, classify_user, write_system, write_user
from .render import join_text, tidy_line
from .stages.publisher import CANDIDATES_PER_SLOT, Publisher, line_text, score


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _line(line: dict | None) -> str:
    line = tidy_line(line or {})
    return join_text(f"**{line.get('lead', '')}**", line.get("rest", ""))


class Bench:
    def __init__(self, cfg, db, size: int):
        self.cfg, self.db, self.size = cfg, db, size

    async def load(self):
        self.items = await self.db.fetch(
            """select r.id, r.source_id, r.title, r.summary, r.fetched_at, r.story_id,
                      s.region, s.topic, s.importance, s.created_at as story_created
               from raw_items r join stories s on s.id = r.story_id
               where r.status in ('accepted', 'duplicate') order by r.id desc limit %s""",
            (self.size,),
        )
        self.items.sort(key=lambda r: r["id"])
        start = min((r["fetched_at"] for r in self.items), default=datetime.now(timezone.utc))
        # The stories that existed when these items came in, as the processor saw them.
        self.recent = await self.db.fetch(
            """select id, title from stories
               where created_at < %s and created_at > %s - make_interval(hours => %s)
               order by id desc limit %s""",
            (start, start, self.cfg.dedupe.window_hours, self.cfg.dedupe.llm_recent_stories),
        )
        self.written = await self.db.fetch(
            """select * from stories where status in ('written', 'posted') and line is not null
                 and not coalesce((line->>'template')::boolean, false)
               order by id desc limit %s""",
            (self.size,),
        )
        self.written.sort(key=lambda r: r["id"])
        rows = await self.db.fetch(
            """select * from stories where status in ('written', 'posted')
                 and created_at > now() - make_interval(hours => %s)""",
            (self.cfg.channel.lookback_hours,),
        )
        now = datetime.now(timezone.utc)
        self.candidates = []
        for sec in self.cfg.sections:
            in_sec = sorted((r for r in rows if r["section"] == sec.id), key=lambda r: score(r, now), reverse=True)
            self.candidates += in_sec[: sec.max_items * CANDIDATES_PER_SLOT]

    async def run(self, llm_cfg, llm) -> list[str]:
        out = [f"## {llm_cfg.provider} · {llm_cfg.model}", ""]
        out += await self._timed("Labelling", self._labelling(llm))
        out += await self._timed("Writing", self._writing(llm))
        out += await self._timed("Grouping", self._grouping(llm))
        return out

    async def _timed(self, title: str, task) -> list[str]:
        t0 = time.monotonic()
        try:
            body = await task
        except LLMError as exc:
            body = [f"**Failed:** {_cell(exc)}"]
        return [f"### {title} ({time.monotonic() - t0:.0f}s)", "", *body, ""]

    async def _labelling(self, llm) -> list[str]:
        if not self.items:
            return ["No processed items in the database."]
        res = await llm.generate(classify_system(self.cfg), classify_user(self.items, self.recent), ItemLabels,
                                 max_tokens=8000)
        labels = {lab.id: lab for lab in res.items}
        recent_ids = {s["id"] for s in self.recent}
        rows, agree = [], {"region": 0, "topic": 0, "merge": 0}
        imp_diff = 0
        for it in self.items:
            lab = labels.get(it["id"])
            if not lab:
                rows.append(f"| {_cell(it['title'][:90])} | missing | | | |")
                continue
            expected = it["story_id"] if it["story_id"] in recent_ids else 0
            got = lab.same_story if lab.same_story in recent_ids else 0
            agree["region"] += lab.region == it["region"]
            agree["topic"] += lab.topic == it["topic"]
            agree["merge"] += got == expected
            imp_diff += abs(lab.importance - it["importance"])
            mark = lambda ok: "" if ok else " ✗"  # noqa: E731
            rows.append(
                f"| {_cell(it['title'][:90])} | {lab.region}{mark(lab.region == it['region'])} "
                f"| {lab.topic}{mark(lab.topic == it['topic'])} | {lab.importance} (was {it['importance']}) "
                f"| {got or '–'}{mark(got == expected)} |"
            )
        n = len(self.items)
        summary = (f"{len(labels)}/{n} labels returned. Agreement with the stored labels: region "
                   f"{agree['region']}/{n}, topic {agree['topic']}/{n}, merge with an older story "
                   f"{agree['merge']}/{n}; importance off by {imp_diff / n:.1f} on average.")
        return [summary, "", "| Item | Region | Topic | Importance | Same story |", "|---|---|---|---|---|", *rows]

    async def _writing(self, llm) -> list[str]:
        if not self.written:
            return ["No written stories in the database."]
        res = await llm.generate(write_system(self.cfg), write_user(self.written), DigestLines, max_tokens=12000)
        lines = {ln.id: ln.model_dump(exclude={"id"}) for ln in res.lines}
        bad_anchor = sum(1 for ln in lines.values() if ln["anchor"] and ln["anchor"] not in ln["rest"])
        rows = [
            f"| {_cell(_line(s['line']))} | {_cell(_line(lines.get(s['id'])) if s['id'] in lines else 'missing')} |"
            for s in self.written
        ]
        summary = f"{len(lines)}/{len(self.written)} lines returned, {bad_anchor} with an anchor not found in the sentence."
        return [summary, "", "| Stored line | New line |", "|---|---|", *rows]

    async def _grouping(self, llm) -> list[str]:
        if not self.candidates:
            return ["No written stories in the lookback window."]
        pub = Publisher(self.cfg, self.db, bot=object(), llm=llm)
        plan = await pub._plan(self.candidates, [])
        by_id = {c["id"]: c for c in self.candidates}
        groups = [ev for ev in plan.events if len([i for i in ev.ids if i in by_id]) > 1]
        listed = [i for ev in plan.events for i in ev.ids]
        unknown = sum(1 for i in listed if i not in by_id)
        missing = len(set(by_id) - set(listed))
        out = [f"{len(self.candidates)} candidates → {len(plan.events)} events, {len(groups)} with several lines. "
               f"{missing} candidates left out of every event, {unknown} made-up ids, "
               f"{len(listed) - len(set(listed))} listed twice.", ""]
        for ev in groups:
            out.append(f"- **{_cell(ev.subject)}** ({ev.region})")
            out += [f"  - {'✓ ' if i == ev.keep else ''}{_cell(line_text(by_id[i]))}" for i in ev.ids if i in by_id]
        return out


async def run_bench(cfg, db, llm_cfgs: list, size: int, build=build_provider) -> str:
    bench = Bench(cfg, db, size)
    await bench.load()
    out = [f"# LLM bench, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", "",
           f"{len(bench.items)} items to label, {len(bench.written)} stories to write, "
           f"{len(bench.candidates)} digest candidates to group.", ""]
    for llm_cfg in llm_cfgs:
        out += await bench.run(llm_cfg, build(llm_cfg))
    return "\n".join(out) + "\n"
