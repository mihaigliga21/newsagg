"""End-to-end run of processor -> writer -> publisher against a real Postgres, with a fake LLM and bot.

Set NEWSAGG_TEST_DATABASE_URL to an empty database to run it (it drops the newsagg schema).
"""

import os
from pathlib import Path

import pytest

from newsagg.config import load_config
from newsagg.db import Database
from newsagg.llm import LLMError
from newsagg.prompts import DigestLine, DigestLines, ItemLabel, ItemLabels
from newsagg.stages import Processor, Publisher, Writer

DB_URL = os.environ.get("NEWSAGG_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="NEWSAGG_TEST_DATABASE_URL not set")
ROOT = Path(__file__).resolve().parent.parent


class FakeLLM:
    def __init__(self, fail_labels=False):
        self.fail_labels = fail_labels

    async def generate(self, system, user, schema, max_tokens=4000):
        import json

        payload = json.loads(user.split("\n", 1)[1])
        if schema is ItemLabels:
            if self.fail_labels:
                raise LLMError("down")
            out = []
            for it in payload:
                t = it["title"]
                region = "world" if "Ucraina" in t else "romania" if "București" in t else "moldova"
                out.append(ItemLabel(id=it["id"], is_news="Horoscop" not in t, region=region,
                                     topic="politics", importance=5 if "Parlament" in t else 3))
            return ItemLabels(items=out)
        if schema is DigestLines:
            return DigestLines(lines=[
                DigestLine(id=s["id"], emoji="🏛", lead=" ".join(s["title"].split()[:3]),
                           rest=" ".join(s["title"].split()[3:]), anchor=" ".join(s["title"].split()[3:5]))
                for s in payload
            ])
        raise AssertionError(schema)


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.next_id = 100

    async def send_message(self, chat_id, text, buttons=None):
        self.next_id += 1
        self.sent.append((chat_id, text, buttons))
        return self.next_id

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    async def answer_callback(self, cid, text=None):
        pass


@pytest.fixture
async def env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "@chan")
    monkeypatch.setenv("TELEGRAM_REVIEW_CHAT_ID", "42")
    cfg = load_config(ROOT / "config" / "config.yaml")
    db = Database(DB_URL)
    async with await __import__("psycopg").AsyncConnection.connect(DB_URL, autocommit=True) as c:
        await c.execute("drop schema if exists newsagg cascade")
    await db.migrate()
    await db.open()
    yield cfg, db
    await db.close()


async def insert(db, source, ext, title, url=None, summary=""):
    await db.execute(
        "insert into raw_items (source_id, external_id, url, title, summary, published_at) values (%s,%s,%s,%s,%s, now())",
        (source, ext, url or f"https://{source}.md/{ext}", title, summary),
    )


async def test_full_pipeline(env):
    cfg, db = env
    await insert(db, "point", "a1", "Parlamentul a votat în ședință secretă noua structură a Armatei")
    await insert(db, "zdg", "b1", "Parlamentul a votat în secret noua structură a Armatei Naționale")  # same story, better source
    await insert(db, "tv8", "c1", "Horoscopul zilei pentru toate zodiile")  # blocklist
    await insert(db, "digi24", "d1", "Atacuri rusești în Ucraina: zeci de morți după lovirea a două autobuze")
    await insert(db, "digi24", "e1", "Primăria București anunță noi restricții de trafic în centru")
    await insert(db, "unimedia", "f1", "Guvernul a aprobat proiectul bugetului de stat pe anul 2027")

    proc = Processor(cfg, db, llm=FakeLLM())
    assert await proc.run_once() == 6
    stories = await db.fetch("select * from stories order by id")
    assert len(stories) == 4
    armata = next(s for s in stories if s["title"].startswith("Parlamentul"))
    assert armata["source_count"] == 2
    assert armata["source_id"] == "zdg"  # priority 1 beats point's priority 3
    assert {s["section"] for s in stories} == {"moldova", "externe", "romania"}
    blocked = await db.fetchone("select status, reject_reason from raw_items where external_id = 'c1'")
    assert blocked == {"status": "rejected", "reject_reason": "blocklist"}

    writer = Writer(cfg, db, llm=FakeLLM())
    assert await writer.run_once() == 4
    assert await writer.run_once() == 0

    bot = FakeBot()
    pub = Publisher(cfg, db, bot=bot)
    digest_id = await pub.build_digest()
    assert digest_id
    review_text = bot.sent[0][1]
    assert "<b>::: EXTERNE</b>" in review_text and "<b>::: ROMÂNIA</b>" in review_text
    assert review_text.index("Parlamentul") < review_text.index("ROMÂNIA") < review_text.index("EXTERNE")
    assert 'href="https://zdg.md/b1"' in review_text
    assert bot.sent[-1][2][0][0]["callback_data"] == f"approve:{digest_id}"

    # A callback from another chat is ignored.
    await pub.handle_update({"callback_query": {"id": "x", "data": f"approve:{digest_id}", "message": {"message_id": 1, "chat": {"id": 7}}}})
    assert (await db.fetchone("select status from digests"))["status"] == "pending_review"

    await pub.handle_update({"callback_query": {"id": "x", "data": f"approve:{digest_id}", "message": {"message_id": 1, "chat": {"id": 42}}}})
    d = await db.fetchone("select * from digests")
    assert d["status"] == "posted"
    assert bot.sent[-1][0] == "@chan"
    posted = await db.fetchone("select count(*) n from stories where status = 'posted'")
    assert posted["n"] == 4

    # Nothing left for the next digest.
    assert await pub.build_digest() is None


async def test_keyword_fallback_and_skip(env):
    cfg, db = env
    await insert(db, "zdg", "k1", "Maia Sandu a convocat Consiliul Suprem de Securitate la Chișinău")
    await insert(db, "digi24_externe", "k2", "China anunță noi tarife pentru importurile europene")
    proc = Processor(cfg, db, llm=FakeLLM(fail_labels=True))
    await proc.run_once()
    rows = {r["title"][:5]: r for r in await db.fetch("select * from stories")}
    assert rows["Maia "]["region"] == "moldova"
    assert rows["China"]["region"] == "world"

    writer = Writer(cfg, db, llm=None)  # template lines
    await writer.run_once()
    line = (await db.fetchone("select line from stories where title like 'Maia%%'"))["line"]
    assert line["template"] is True

    bot = FakeBot()
    pub = Publisher(cfg, db, bot=bot)
    digest_id = await pub.build_digest()
    await pub.handle_update({"callback_query": {"id": "x", "data": f"skip:{digest_id}", "message": {"message_id": 1, "chat": {"id": 42}}}})
    assert (await db.fetchone("select status from digests"))["status"] == "skipped"
    # Skipped stories are free again for a rebuilt digest.
    assert await pub.build_digest() is not None
