"""Postgres access: connection pool, migrations and LISTEN/NOTIFY waits.

On Supabase use the *session pooler* or direct connection string (port 5432).
The transaction pooler (port 6543) does not support LISTEN.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def _configure(conn: psycopg.AsyncConnection):
    async def _inner():
        await conn.execute("set search_path to newsagg, public")
        await conn.commit()

    return _inner()


class Database:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 4):
        self.url = url
        self.pool = AsyncConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            configure=_configure,
        )

    async def open(self):
        await self.pool.open(wait=True)

    async def close(self):
        await self.pool.close()

    @asynccontextmanager
    async def tx(self):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                yield conn

    async def fetch(self, sql: str, params=None) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def fetchone(self, sql: str, params=None) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def execute(self, sql: str, params=None) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    async def migrate(self, attempts: int = 12):
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        for i in range(attempts):
            try:
                conn = await psycopg.AsyncConnection.connect(self.url, autocommit=True)
                break
            except psycopg.OperationalError as exc:
                if i == attempts - 1:
                    raise
                log.info("database not reachable yet (%s); retrying", exc)
                await asyncio.sleep(5)
        async with conn:
            await conn.execute(sql)
        log.info("schema applied")

    async def kv_get(self, key: str, default=None):
        row = await self.fetchone("select value from kv where key = %s", (key,))
        return row["value"] if row else default

    async def kv_set(self, key: str, value):
        from psycopg.types.json import Jsonb

        await self.execute(
            "insert into kv (key, value) values (%s, %s) on conflict (key) do update set value = excluded.value",
            (key, Jsonb(value)),
        )


class Notifier:
    """Holds one dedicated LISTEN connection and lets a stage wait for a channel or a timeout."""

    def __init__(self, url: str, channels: list[str]):
        self.url = url
        self.channels = channels
        self._event = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self.url, autocommit=True) as conn:
                    for ch in self.channels:
                        await conn.execute(f"listen {ch}")
                    log.debug("listening on %s", self.channels)
                    async for n in conn.notifies():
                        log.debug("notify on %s", n.channel)
                        self._event.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reconnect; the poll timeout keeps work moving meanwhile
                log.warning("LISTEN connection lost (%s); retrying in 10s", exc)
                await asyncio.sleep(10)

    async def wait(self, timeout: float):
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._event.clear()

    async def stop(self):
        if self._task:
            self._task.cancel()
