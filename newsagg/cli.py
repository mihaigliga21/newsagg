"""Command line entry point.

  newsagg migrate                 create or update the database schema
  newsagg run <stage>|all         run collector, processor, writer, publisher (or all of them)
  newsagg test-source <id>        fetch one source and print what it returns (no database)
  newsagg digest --dry-run        print the digest that would be built now (no Telegram)
  newsagg digest                  build a digest now and send it for review
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import httpx

from .config import load_config
from .db import Database


def _setup_logging():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "anthropic", "psycopg", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _run(cfg, stage_names: list[str]):
    from .stages import STAGES

    db = Database(cfg.database_url, max_size=2 + 2 * len(stage_names))
    await db.open()
    try:
        stages = [STAGES[name](cfg, db) for name in stage_names]
        await asyncio.gather(*(s.run_forever() for s in stages))
    finally:
        await db.close()


async def _migrate(cfg):
    await Database(cfg.database_url).migrate()


async def _test_source(cfg, source_id: str):
    from .sources import build_source
    from .sources.base import USER_AGENT

    src = cfg.source(source_id)
    if not src:
        sys.exit(f"unknown source {source_id!r}")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as http:
        result = await build_source(src, http).fetch()
    for it in result.items:
        print(f"- {it.published_at or '?'} | {it.title}\n  {it.url}")
    print(f"{len(result.items)} items")


async def _digest(cfg, dry_run: bool):
    from .stages.publisher import Publisher

    db = Database(cfg.database_url)
    await db.open()
    try:
        pub = Publisher(cfg, db)
        if dry_run:
            for part in pub.render(await pub.select_stories()):
                print(part, end="\n\n---\n\n")
        else:
            await pub.build_digest()
    finally:
        await db.close()


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(prog="newsagg")
    parser.add_argument("--config", default=os.environ.get("NEWSAGG_CONFIG", "config/config.yaml"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    run = sub.add_parser("run")
    run.add_argument("stage", choices=["collector", "processor", "writer", "publisher", "all"])
    ts = sub.add_parser("test-source")
    ts.add_argument("source_id")
    dg = sub.add_parser("digest")
    dg.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging()
    cfg = load_config(args.config)
    if args.cmd == "migrate":
        asyncio.run(_migrate(cfg))
    elif args.cmd == "run":
        names = ["collector", "processor", "writer", "publisher"] if args.stage == "all" else [args.stage]
        asyncio.run(_run(cfg, names))
    elif args.cmd == "test-source":
        asyncio.run(_test_source(cfg, args.source_id))
    elif args.cmd == "digest":
        asyncio.run(_digest(cfg, args.dry_run))


if __name__ == "__main__":
    main()
