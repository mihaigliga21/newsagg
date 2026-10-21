from __future__ import annotations

import asyncio
import logging

from ..config import Config
from ..db import Database, Notifier


DEFAULT = object()  # "build the LLM provider from config"; tests pass a fake or None


class Stage:
    """A pipeline stage. It processes whatever the previous stage left, then sleeps until
    a NOTIFY on one of `listen` arrives or `poll_seconds` passes."""

    name = "stage"
    listen: list[str] = []

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.log = logging.getLogger(f"newsagg.{self.name}")

    async def recover(self):
        """Release rows a crashed run left claimed."""

    async def run_once(self) -> int:
        raise NotImplementedError

    async def run_forever(self):
        await self.recover()
        notifier = Notifier(self.db.url, self.listen) if self.listen else None
        if notifier:
            await notifier.start()
        self.log.info("started")
        try:
            while True:
                try:
                    done = await self.run_once()
                except Exception:
                    self.log.exception("run failed")
                    done = 0
                if done:
                    continue  # there may be more waiting
                if notifier:
                    await notifier.wait(self.cfg.worker.poll_seconds)
                else:
                    await asyncio.sleep(self.cfg.worker.poll_seconds)
        finally:
            if notifier:
                await notifier.stop()
