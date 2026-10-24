"""Stage 3: write the digest line (emoji, bold lead, sentence, link anchor) for each new story."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from ..llm import LLMError, build_provider
from ..prompts import DigestLines, write_system, write_user
from ..render import template_line
from .base import DEFAULT, Stage

MAX_ATTEMPTS = 3


class Writer(Stage):
    name = "writer"
    listen = ["newsagg_stories"]

    def __init__(self, cfg, db, llm=DEFAULT):
        super().__init__(cfg, db)
        if llm is DEFAULT:
            llm = build_provider(cfg.llm) if cfg.llm.enabled and cfg.llm.write_lines else None
        self.llm = llm

    async def recover(self):
        await self.db.execute("update stories set status = 'pending_write' where status = 'writing'")

    async def run_once(self) -> int:
        stories = sorted(await self.db.fetch(
            """update stories set status = 'writing', attempts = attempts + 1
               where id in (select id from stories where status = 'pending_write' order by id
                            limit %s for no key update skip locked)
               returning *""",
            (self.cfg.llm.batch_size,),
        ), key=lambda r: r["id"])
        if not stories:
            return 0

        lines: dict[int, dict] = {}
        if self.llm:
            try:
                res = await self.llm.generate(
                    write_system(self.cfg), write_user(stories), DigestLines, max_tokens=12000
                )
                lines = {ln.id: ln.model_dump(exclude={"id"}) for ln in res.lines}
            except LLMError as exc:
                self.log.warning("LLM writing failed for %d stories: %s", len(stories), exc)

        written = 0
        for s in stories:
            line = lines.get(s["id"])
            if line is None and self.llm and s["attempts"] < MAX_ATTEMPTS:
                await self.db.execute("update stories set status = 'pending_write' where id = %s", (s["id"],))
                continue
            if line is None:
                emoji = self.cfg.topics.get(s["topic"] or "default", self.cfg.topics["default"]).emoji
                line = template_line(s["title"], emoji) | {"template": True}
            await self.db.execute(
                "update stories set status = 'written', line = %s, updated_at = now() where id = %s",
                (Jsonb(line), s["id"]),
            )
            written += 1
        self.log.info("wrote %d of %d lines", written, len(stories))
        # Returning 0 when nothing was written avoids a hot loop on a failing LLM;
        # the retried stories are picked up on the next notify or poll.
        return written
