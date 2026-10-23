"""End-to-end run of processor -> writer -> publisher against a real Postgres, with a fake LLM and bot.

Set NEWSAGG_TEST_DATABASE_URL to an empty database to run it (it drops the newsagg schema).
"""

import os
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from newsagg.config import load_config
from newsagg.db import Database
from newsagg.llm import LLMError
from newsagg.prompts import DigestEvent, DigestLine, DigestLines, DigestPlan, ItemLabel, ItemLabels
from newsagg.stages import Processor, Publisher, Writer
from newsagg.textutil import normalize_title

DB_URL = os.environ.get("NEWSAGG_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="NEWSAGG_TEST_DATABASE_URL not set")
ROOT = Path(__file__).resolve().parent.parent


class FakeLLM:
    def __init__(self, fail_labels=False, fail_plan=False):
        self.fail_labels = fail_labels
        self.fail_plan = fail_plan
        self.plan_calls = 0

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
        if schema is DigestPlan:
            # Same event = same first word ("Putin ..."); subject = the person named in the line.
            self.plan_calls += 1
            if self.fail_plan:
                raise LLMError("down")
            self.sent = payload["already_sent"]
            groups, sections = {}, {}
            for c in payload["candidates"]:
                groups.setdefault(c["line"].split()[0], []).append(c["id"])
                sections[c["id"]] = c["section"]
            region = lambda i: {"externe": "world"}.get(sections[i], sections[i])
            sent_keys = {line.split()[0] for line in self.sent}
            events = []
            for key, ids in groups.items():
                if key.startswith("Niculescu"):  # different happenings, same person
                    events += [DigestEvent(ids=[i], keep=i, subject="Luca Niculescu", region=region(i),
                                           already_sent=False) for i in ids]
                else:
                    events.append(DigestEvent(ids=ids, keep=max(ids), subject=key, region=region(ids[0]),
                                              already_sent=key in sent_keys))
            return DigestPlan(events=events)
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
    pub = Publisher(cfg, db, bot=bot, llm=FakeLLM())
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
    pub = Publisher(cfg, db, bot=bot, llm=None)
    digest_id = await pub.build_digest()
    await pub.handle_update({"callback_query": {"id": "x", "data": f"skip:{digest_id}", "message": {"message_id": 1, "chat": {"id": 42}}}})
    assert (await db.fetchone("select status from digests"))["status"] == "skipped"
    # Skipped stories are free again for a rebuilt digest.
    assert await pub.build_digest() is not None


async def add_story(db, section, title, source_count=1, importance=4):
    words = title.split()
    line = {"emoji": "💥", "lead": " ".join(words[:3]), "rest": " ".join(words[3:]), "anchor": " ".join(words[3:5])}
    row = await db.fetchone(
        """insert into stories (norm_title, title, url, source_id, source_count, region, section, topic,
                                importance, status, line)
           values (%s, %s, %s, 'digi24', %s, %s, %s, 'politics', %s, 'written', %s) returning id""",
        (normalize_title(title), title, f"https://digi24.ro/{len(title)}{words[-1]}", source_count,
         {"externe": "world"}.get(section, section), section, importance, Jsonb(line)),
    )
    return row["id"]


async def seed_repetitive_day(db):
    ids = {}
    ids["putin1"] = await add_story(db, "externe", "Putin acceptă să reia negocierile de pace cu Ucraina și SUA", 3)
    ids["putin2"] = await add_story(db, "externe", "Putin a acceptat reluarea negocierilor cu Ucraina din luna viitoare", 2)
    ids["putin3"] = await add_story(db, "externe", "Putin acceptă cererea lui Trump privind negocierile", 2)
    ids["zelenski"] = await add_story(db, "externe", "Zelenski ia în calcul înghețarea conflictului pe linia frontului", 1)
    ids["china"] = await add_story(db, "externe", "China anunță noi tarife pentru importurile europene", 1, importance=3)
    for n, t in enumerate([
        "Niculescu prezintă un plan pentru siguranța elevilor și profesorilor",
        "Niculescu anunță trei priorități pentru fondurile europene",
        "Niculescu a fost criticat că nu a stat în capul mesei",
    ]):
        ids[f"nic{n}"] = await add_story(db, "romania", t, 3 - n)
    ids["prefect"] = await add_story(db, "romania", "Prefectul Capitalei a demisionat în semn de protest", 1, importance=3)
    return ids


async def test_digest_keeps_one_line_per_event(env):
    cfg, db = env
    ids = await seed_repetitive_day(db)
    bot, llm = FakeBot(), FakeLLM()
    pub = Publisher(cfg, db, bot=bot, llm=llm)
    digest_id = await pub.build_digest()
    assert llm.plan_calls == 1
    text = bot.sent[0][1]
    assert text.count("Putin") == 1  # three reports of the same event -> one line
    assert "Zelenski" in text and "China" in text
    assert text.count("Niculescu") == 2  # max_per_subject
    assert "Prefectul" in text

    d = await db.fetchone("select story_ids from digests where id = %s", (digest_id,))
    assert ids["putin3"] in d["story_ids"]  # the LLM's pick
    assert ids["putin1"] not in d["story_ids"]
    # Folded duplicates go with the digest; the third Niculescu line stays free.
    in_digest = {r["id"] for r in await db.fetch("select id from stories where digest_id = %s", (digest_id,))}
    assert {ids["putin1"], ids["putin2"]} <= in_digest
    assert ids["nic2"] not in in_digest

    await pub.post(digest_id)
    posted = {r["id"] for r in await db.fetch("select id from stories where status = 'posted'")}
    assert ids["putin1"] in posted and ids["nic2"] not in posted


async def test_digest_falls_back_to_ranking_when_llm_fails(env):
    cfg, db = env
    ids = await seed_repetitive_day(db)
    bot = FakeBot()
    pub = Publisher(cfg, db, bot=bot, llm=FakeLLM(fail_plan=True))
    digest_id = await pub.build_digest()
    d = await db.fetchone("select story_ids from digests where id = %s", (digest_id,))
    # Plain ranking, with near-identical titles still merged: one Putin line, then the rest by score.
    assert ids["putin1"] in d["story_ids"]
    assert ids["putin2"] not in d["story_ids"] and ids["putin3"] not in d["story_ids"]
    assert {ids["zelenski"], ids["china"]} <= set(d["story_ids"])
    assert len([i for i in d["story_ids"] if i in {ids[f"nic{n}"] for n in range(3)}]) == 3  # no subjects without the LLM


async def test_next_digest_does_not_repeat_events(env):
    cfg, db = env
    ids = await seed_repetitive_day(db)
    bot, llm = FakeBot(), FakeLLM()
    pub = Publisher(cfg, db, bot=bot, llm=llm)
    first = await pub.build_digest()
    await pub.post(first)

    # Two hours later: a new report of the Putin event and one genuinely new story.
    putin4 = await add_story(db, "externe", "Putin confirmă reluarea negocierilor după întâlnirea cu Witkoff", 2)
    iran = await add_story(db, "externe", "Iranul răspunde la amenințările SUA și ale Israelului", 1)
    bot.sent.clear()
    second = await pub.build_digest()
    assert any(line.startswith("Putin") for line in llm.sent)
    d = await db.fetchone("select story_ids from digests where id = %s", (second,))
    assert iran in d["story_ids"] and putin4 not in d["story_ids"]
    assert ids["nic2"] in d["story_ids"]  # left over from the first digest, not a repeat
    assert "Am omis 1 știri deja trimise" in bot.sent[-1][1]


async def test_quiet_hour_sends_no_digest(env):
    cfg, db = env
    await add_story(db, "externe", "Hamas amenință Israelul cu un nou atac de amploarea celui de la 7 octombrie", 2)
    bot = FakeBot()
    pub = Publisher(cfg, db, bot=bot, llm=None)
    await pub.post(await pub.build_digest())
    await add_story(db, "externe", "Hamas amenință Israelul cu noi atacuri, similare masacrului din 7 octombrie", 2)
    assert await pub.build_digest() is None
    assert bot.sent[-1][1].startswith("Nu sunt știri noi pentru digest.\nAm omis 1")


async def test_grouping_failure_is_shown_in_review(env):
    cfg, db = env
    await seed_repetitive_day(db)
    bot = FakeBot()
    await Publisher(cfg, db, bot=bot, llm=FakeLLM(fail_plan=True)).build_digest()
    assert "⚠️ Gruparea pe evenimente a eșuat (down)" in bot.sent[-1][1]


async def kill_pool_connections(db):
    """What Supabase/Supavisor does to idle sessions: the server ends them (AdminShutdown)."""
    async with await __import__("psycopg").AsyncConnection.connect(DB_URL, autocommit=True) as c:
        await c.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = current_database() and pid <> pg_backend_pid()"
        )


async def test_queries_survive_server_closing_idle_connections(env):
    cfg, db = env
    await add_story(db, "externe", "Hamas amenință Israelul cu un nou atac", 2)
    await kill_pool_connections(db)
    assert await db.fetchone("select count(*) n from stories") == {"n": 1}
    # A connection that dies between the pool check and the query is retried too.
    async with db.pool.connection() as conn:
        pid = conn.info.backend_pid
    db.pool._check = None  # skip the pool check so the query hits the dead connection
    async with await __import__("psycopg").AsyncConnection.connect(DB_URL, autocommit=True) as c:
        await c.execute("select pg_terminate_backend(%s)", (pid,))
    assert await db.fetchone("select count(*) n from stories") == {"n": 1}


async def test_scheduled_digest_retries_until_the_database_is_back(env):
    cfg, db = env
    await add_story(db, "externe", "Hamas amenință Israelul cu un nou atac", 2)
    bot = FakeBot()
    pub = Publisher(cfg, db, bot=bot, llm=None)
    pub.retry_delays = (0, 0)
    real, calls = pub.select_stories, []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise __import__("psycopg").errors.AdminShutdown("terminating connection due to administrator command")
        return await real()

    pub.select_stories = flaky
    await pub.scheduled_digest(pub.now())
    assert len(calls) == 3
    assert (await db.fetchone("select count(*) n from digests"))["n"] == 1


async def test_scheduled_digest_reports_when_retries_run_out(env):
    cfg, db = env
    bot = FakeBot()
    pub = Publisher(cfg, db, bot=bot, llm=None)
    pub.retry_delays = (0,)

    async def down():
        raise __import__("psycopg").OperationalError("server closed the connection")

    pub.select_stories = down
    await pub.scheduled_digest(pub.now())
    assert bot.sent[-1][1].startswith("⚠️ Digestul programat nu a putut fi construit")


async def test_scheduled_digest_is_not_rebuilt_after_it_was_saved(env):
    cfg, db = env
    await add_story(db, "externe", "Hamas amenință Israelul cu un nou atac", 2)
    bot = FakeBot()
    pub = Publisher(cfg, db, bot=bot, llm=None)
    pub.retry_delays = (0, 0)

    async def telegram_down(*a, **k):
        raise RuntimeError("telegram down")

    pub.send_for_review = telegram_down
    await pub.scheduled_digest(pub.now())
    assert (await db.fetchone("select count(*) n from digests"))["n"] == 1


def scheduled_publisher(cfg, db, now, bot=None):
    """A publisher with digests at 09:00, 12:00 and 17:00 and a fixed clock."""
    from datetime import datetime as dt

    cfg.channel.schedule = ["0 9 * * *", "0 12 * * *", "0 17 * * *"]
    pub = Publisher(cfg, db, bot=bot or FakeBot(), llm=None)
    pub.retry_delays = ()
    pub.now = lambda: dt.fromisoformat(now).astimezone(pub.tz)
    return pub


async def digest_count(db):
    return (await db.fetchone("select count(*) n from digests"))["n"]


async def test_restart_catches_up_todays_missed_slots_in_one_digest(env):
    cfg, db = env
    await add_story(db, "externe", "Hamas amenință Israelul cu un nou atac", 2)
    pub = scheduled_publisher(cfg, db, "2026-10-23T18:00:00+03:00")
    await db.kv_set("digest_last_slot", "2026-10-21T17:00:00+03:00")  # yesterday's slots are dropped
    await pub.catch_up()
    assert await digest_count(db) == 1
    assert "Include și digestele ratate de la 09:00, 12:00, 17:00." in pub.bot.sent[-1][1]
    assert await db.kv_get("digest_last_slot") == "2026-10-23T17:00:00+03:00"
    await pub.catch_up()  # cleared: a second restart builds nothing
    assert await digest_count(db) == 1


async def test_restart_leaves_missed_slot_to_a_close_next_slot(env):
    cfg, db = env
    await add_story(db, "externe", "Hamas amenință Israelul cu un nou atac", 2)
    pub = scheduled_publisher(cfg, db, "2026-10-23T16:45:00+03:00")
    await db.kv_set("digest_last_slot", "2026-10-23T09:00:00+03:00")
    await pub.catch_up()
    assert await digest_count(db) == 0
    await pub.scheduled_digest(pub.next_slot(pub.now()))  # the 17:00 digest covers 12:00
    assert "Include și digestul ratat de la 12:00." in pub.bot.sent[-1][1]
    assert await db.kv_get("digest_last_slot") == "2026-10-23T17:00:00+03:00"


async def test_first_start_uses_newest_digest_to_find_missed_slots(env):
    cfg, db = env
    pub = scheduled_publisher(cfg, db, "2026-10-23T13:00:00+03:00")
    await pub.catch_up()  # never a digest: nothing to catch up, just start counting
    assert await digest_count(db) == 0
    assert await db.kv_get("digest_last_slot") == "2026-10-23T12:00:00+03:00"

    await db.execute("delete from kv where key = 'digest_last_slot'")
    await db.execute("insert into digests (created_at, status, parts, story_ids) "
                     "values ('2026-10-23T09:00:05+03:00', 'posted', '[]', '{}')")
    await add_story(db, "externe", "Hamas amenință Israelul cu un nou atac", 2)
    await pub.catch_up()
    assert "Include și digestul ratat de la 12:00." in pub.bot.sent[-1][1]


async def test_failed_slot_stays_missed(env):
    cfg, db = env
    pub = scheduled_publisher(cfg, db, "2026-10-23T12:00:00+03:00")
    await db.kv_set("digest_last_slot", "2026-10-23T09:00:00+03:00")

    async def down():
        raise __import__("psycopg").OperationalError("server closed the connection")

    pub.select_stories = down
    await pub.scheduled_digest(pub.now())
    assert await db.kv_get("digest_last_slot") == "2026-10-23T09:00:00+03:00"
