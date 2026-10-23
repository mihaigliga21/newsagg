from pathlib import Path

from newsagg.classify import KeywordClassifier
from newsagg.config import load_config
from newsagg.dedupe import find_match
from newsagg.prompts import DigestEvent, DigestPlan, write_user
from newsagg.render import render_digest, render_line, split_blocks, template_line, tidy_line, visible_length
from newsagg.sources.rss import parse_feed
from newsagg.sources.telegram_web import parse_channel_page
from newsagg.stages.publisher import apply_plan
from newsagg.textutil import canonical_url, normalize_title, strip_media_tags

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"


def cfg(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "@c")
    return load_config(ROOT / "config" / "config.yaml")


def test_config_loads(monkeypatch):
    c = cfg(monkeypatch)
    assert c.database_url == "postgresql://x"
    assert c.channel.review_chat_id == ""  # unset env var -> empty -> posts directly
    assert c.section_for_region("world") == "externe"
    assert c.section_for_region("other") is None
    assert c.llm.base_url == ""
    assert [s.max_items for s in c.sections] == [6, 4, 3]
    assert c.channel.max_per_subject == 2 and c.llm.curate


def test_canonical_url():
    assert canonical_url("HTTPS://Site.md/a/b/?utm_source=x&id=3&fbclid=9#top") == "https://site.md/a/b?id=3"


def test_normalize_title_folds_diacritics_and_cedillas():
    assert normalize_title("Șeful „Moldovagaz”: tarifele cresc") == normalize_title("Seful Moldovagaz - tarifele cresc")
    assert normalize_title("Ţară şi ţară") == "tara si tara"


def test_keyword_classifier(monkeypatch):
    k = KeywordClassifier(cfg(monkeypatch))
    assert k.region("Maia Sandu a convocat Consiliul Suprem de Securitate", None, None) == "moldova"
    assert k.region("Atacuri rusești în Ucraina: 30 de morți", None, "romania") == "world"
    assert k.region("Guvernul a aprobat bugetul", None, "romania") == "romania"
    assert k.region("Pași mici spre reformă", None, None) is None  # "PAS" must not match "pași"
    assert k.topic("Procurorii au deschis un dosar penal", None) == "justice"
    assert k.is_blocked("Horoscopul zilei de 8 octombrie")


def test_find_match():
    cands = [
        (1, normalize_title("Parlamentul a votat în ședință secretă noua structură a Armatei")),
        (2, normalize_title("Maia Sandu: Ambasada Moldovei la Moscova rămâne deschisă")),
    ]
    assert find_match("Parlamentul a votat în secret noua structură a Armatei Naționale", cands, 80, 4) == 1
    assert find_match("Guvernul a aprobat proiectul bugetului pe 2027", cands, 80, 4) is None
    assert find_match("Maia Sandu", cands, 80, 4) is None  # too short to trust


def test_render_line_links_anchor():
    line = {"emoji": "🏛", "lead": "Parlamentul a votat în ședință secretă", "rest": "noua structură a Armatei, cu majorarea efectivului", "anchor": "a Armatei"}
    out = render_line(line, "https://zdg.md/x?a=1&b=2")
    assert out == '🏛 <b>Parlamentul a votat în ședință secretă</b> noua structură <a href="https://zdg.md/x?a=1&amp;b=2">a Armatei</a>, cu majorarea efectivului'


def test_render_line_escapes_and_falls_back():
    line = {"emoji": "⚖️", "lead": "A <b>bad</b> lead", "rest": "text where anchor is missing", "anchor": "nope"}
    out = render_line(line, "https://x.md/1")
    assert "&lt;b&gt;bad&lt;/b&gt;" in out
    assert out.endswith('<a href="https://x.md/1">is missing</a>')


def test_template_line():
    t = template_line("Ion Chicu acuză tergiversarea: procedura de suspendare a Maiei Sandu", "⚖️")
    assert t["lead"] == "Ion Chicu acuză tergiversarea:"
    assert t["anchor"] in t["rest"]


def test_split_respects_limit():
    blocks = ["x" * 1500 for _ in range(6)]
    parts = split_blocks(blocks, limit=4096)
    assert all(len(p) <= 4096 for p in parts)
    assert sum(p.count("x") for p in parts) == 9000
    assert len(parts) == 3


def test_render_digest_sections():
    parts = render_digest([(None, ["a"]), ("::: EXTERNE", ["b"]), ("::: EMPTY", [])], header="H")
    assert parts == ["H\n\na\n\n<b>::: EXTERNE</b>\n\nb"]


def test_parse_feed():
    items = parse_feed((FIX / "feed.xml").read_bytes())
    assert len(items) == 2
    assert items[0].title == "Consiliul UE prelungește sancțiunile"
    assert items[0].url == "https://www.zdg.md/stiri/consiliul-ue"
    assert items[0].published_at is not None
    assert "<p>" not in items[0].summary


def test_parse_telegram_page():
    items = parse_channel_page((FIX / "tg.html").read_text(encoding="utf-8"), "chan")
    assert [i.external_id for i in items] == ["tg:chan/101", "tg:chan/102"]
    assert items[0].url == "https://newsmaker.md/ro/articol"
    assert items[1].url == "https://t.me/chan/102"
    assert items[0].title.startswith("Guvernul a aprobat")


def test_strip_media_tags():
    assert strip_media_tags("VIDEO/ Compania Națională de drumuri") == "Compania Națională de drumuri"
    assert strip_media_tags("(video) Costiuc: „Un bărbat”") == "Costiuc: „Un bărbat”"
    assert strip_media_tags("FOTO | Podul de la Ungheni [VIDEO]") == "Podul de la Ungheni"
    assert strip_media_tags("Atac la Kiev - VIDEO") == "Atac la Kiev"
    assert strip_media_tags("LIVE: Putin acceptă negocierile") == "Putin acceptă negocierile"
    assert strip_media_tags("Livezile din Ungheni, video de la fața locului") == "Livezile din Ungheni, video de la fața locului"


def test_writer_input_has_no_media_tags():
    assert "VIDEO" not in write_user([{"id": 1, "title": "VIDEO/ Podul de la Ungheni", "summary": ""}])


def test_tidy_line_moves_dangling_words_out_of_lead_and_anchor():
    t = tidy_line({"lead": "Măsuri de securitate la", "rest": "granița cu Ucraina", "anchor": "parcursul european al"})
    assert t["lead"] == "Măsuri de securitate"
    assert t["rest"] == "la granița cu Ucraina"
    assert t["anchor"] == "parcursul european"


def test_tidy_line_keeps_quotes_whole_and_strips_tags():
    t = tidy_line({"lead": "(video) Luca Niculescu anunță „un", "rest": "plan concret” pentru elevi", "anchor": "pentru elevi"})
    assert t["lead"] == "Luca Niculescu anunță"
    assert t["rest"] == "„un plan concret” pentru elevi"


def test_render_line_no_space_before_punctuation():
    line = {"emoji": "💥", "lead": "Rusia respinge propunerile europenilor", "rest": ", formulate la Oslo , pentru negocieri",
            "anchor": "la Oslo"}
    out = render_line(line, "https://x.md/1")
    assert out == '💥 <b>Rusia respinge propunerile europenilor</b>, formulate <a href="https://x.md/1">la Oslo</a>, pentru negocieri'


_TITLES = [
    "", "Guvernul aprobă bugetul pentru spitale", "Elevii protestează față de noile manuale",
    "Ploile au inundat trei sate din nord", "Banca centrală ridică dobânda de referință",
    "Kievul raportează un atac cu drone", "Alegeri anticipate convocate în Bulgaria",
    "Prețul gazelor scade pe bursa europeană",
]


def _story(sid, section, title=None):
    return {"id": sid, "section": section, "norm_title": normalize_title(title or _TITLES[sid])}


def _ev(ids, keep, subject, region="romania", already_sent=False):
    return DigestEvent(ids=ids, keep=keep, subject=subject, region=region, already_sent=already_sent)


def test_apply_plan_one_line_per_event_and_subject_cap(monkeypatch):
    c = cfg(monkeypatch)
    ranked = {"moldova": [], "romania": [_story(1, "romania"), _story(2, "romania"), _story(3, "romania"), _story(4, "romania")],
              "externe": [_story(5, "externe"), _story(6, "externe"), _story(7, "externe")]}
    scores = {1: 50, 2: 49, 3: 48, 4: 10, 5: 40, 6: 39, 7: 20}
    plan = DigestPlan(events=[
        _ev([1], 1, "Luca Niculescu"),
        _ev([2], 2, " LUCA  Niculescu"),  # same subject after normalizing
        _ev([3], 3, "luca niculescu"),
        _ev([5, 6, 99], 6, "Vladimir Putin", "world"),  # 99 is unknown and ignored
    ])
    sel = apply_plan(c, ranked, plan, scores)
    assert [s["id"] for s in sel.by_section["romania"]] == [1, 2, 4]  # 3 is a third Niculescu line
    assert [s["id"] for s in sel.by_section["externe"]] == [6, 7]
    assert sel.folded == [5]


def test_apply_plan_moves_event_to_the_section_of_its_region(monkeypatch):
    c = cfg(monkeypatch)
    ranked = {"moldova": [_story(1, "moldova")], "romania": [_story(2, "romania")], "externe": []}
    plan = DigestPlan(events=[_ev([1], 1, "a", "moldova"), _ev([2], 2, "b", "moldova")])
    sel = apply_plan(c, ranked, plan, {1: 2, 2: 1})
    assert [s["id"] for s in sel.by_section["moldova"]] == [1, 2] and sel.by_section["romania"] == []


def test_apply_plan_skips_events_already_sent(monkeypatch):
    c = cfg(monkeypatch)
    ranked = {"moldova": [_story(1, "moldova", "Parlamentul a adoptat politica fiscală pentru 2027"),
                          _story(2, "moldova", "Un elev de 14 ani a fost rănit cu un cuțit într-o școală"),
                          _story(3, "moldova", "Stațiile de transport public vor fi mutate lângă școli")],
              "romania": [], "externe": []}
    plan = DigestPlan(events=[_ev([1], 1, "Parlamentul", "moldova", already_sent=True),
                              _ev([2], 2, "Poliția", "moldova"), _ev([3], 3, "Guvernul", "moldova")])
    sent = [normalize_title("Un elev de 14 ani a fost rănit cu un cuțit într-o școală din Chișinău")]
    sel = apply_plan(c, ranked, plan, {1: 3, 2: 2, 3: 1}, sent)
    assert [s["id"] for s in sel.by_section["moldova"]] == [3]  # 1 by the LLM, 2 by title
    assert sel.repeats == 2 and sorted(sel.folded) == [1, 2]


def test_apply_plan_without_plan_merges_similar_titles(monkeypatch):
    c = cfg(monkeypatch)
    ranked = {"moldova": [], "romania": [], "externe": [
        _story(1, "externe", "Hamas amenință Israelul cu un nou atac de amploarea celui de la 7 octombrie"),
        _story(2, "externe", "Hamas amenință Israelul cu noi atacuri, similare masacrului din 7 octombrie"),
        _story(3, "externe", "SUA și Israelul pregătesc o nouă ofensivă împotriva Iranului"),
    ]}
    sel = apply_plan(c, ranked, None, {1: 3, 2: 2, 3: 1})
    assert [s["id"] for s in sel.by_section["externe"]] == [1, 3] and sel.folded == [2]


def test_apply_plan_bad_keep_falls_back_to_best_ranked(monkeypatch):
    c = cfg(monkeypatch)
    ranked = {"moldova": [], "romania": [_story(1, "romania"), _story(2, "romania")], "externe": []}
    plan = DigestPlan(events=[_ev([2, 1], 42, "x"), _ev([1], 1, "y")])
    sel = apply_plan(c, ranked, plan, {1: 2, 2: 1})
    assert [s["id"] for s in sel.by_section["romania"]] == [1] and sel.folded == [2]


async def test_openai_compatible_returns_digest_plan():
    import json

    import httpx

    from newsagg.config import LLMConfig
    from newsagg.llm.openai_compatible import OpenAICompatibleProvider

    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        event = {"ids": [1, 2], "keep": 2, "subject": "Vladimir Putin", "region": "world", "already_sent": False}
        content = "```json\n" + json.dumps({"events": [event]}) + "\n```"  # fenced, as some models do
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    prov = OpenAICompatibleProvider(LLMConfig(provider="openai_compatible", base_url="https://llm.test/v1", api_key="k"))
    prov.http = httpx.AsyncClient(base_url="https://llm.test/v1", transport=httpx.MockTransport(handler))
    plan = await prov.generate("s", "u", DigestPlan)
    assert plan.events[0].keep == 2
    assert sent["response_format"]["json_schema"]["name"] == "DigestPlan"
    fmt = sent["response_format"]["json_schema"]
    assert fmt["strict"] is True
    event_schema = fmt["schema"]["$defs"]["DigestEvent"]
    assert event_schema["additionalProperties"] is False and len(event_schema["required"]) == 5


def _provider(replies):
    import httpx

    from newsagg.config import LLMConfig
    from newsagg.llm.openai_compatible import OpenAICompatibleProvider

    calls = []

    def handler(request):
        content, finish = replies[len(calls)]
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]})

    prov = OpenAICompatibleProvider(LLMConfig(provider="openai_compatible", base_url="https://llm.test/v1", api_key="k"))
    prov.http = httpx.AsyncClient(base_url="https://llm.test/v1", transport=httpx.MockTransport(handler))
    return prov, calls


async def test_openai_compatible_repairs_unescaped_quotes():
    # The model closed a Romanian quote with an ASCII one, which breaks the JSON string.
    bad = ('{"events": [{"ids": [1, 2], "keep": 2, "subject": "Partidul „Șor" interzis", '
           '"region": "moldova", "already_sent": false}]}')
    prov, calls = _provider([(bad, "stop")])
    plan = await prov.generate("s", "u", DigestPlan)
    assert plan.events[0].ids == [1, 2] and len(calls) == 1


async def test_openai_compatible_asks_again_when_reply_does_not_fit():
    import json

    good = json.dumps({"events": [{"ids": [1], "keep": 1, "subject": "x", "region": "world", "already_sent": False}]})
    prov, calls = _provider([("Sorry, I can't help with that.", "stop"), (good, "stop")])
    plan = await prov.generate("s", "u", DigestPlan)
    assert plan.events[0].keep == 1 and len(calls) == 2


async def test_openai_compatible_drops_strict_mode_when_rejected():
    import json

    import httpx

    from newsagg.config import LLMConfig
    from newsagg.llm.openai_compatible import OpenAICompatibleProvider

    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body["response_format"]["json_schema"].get("strict"):
            return httpx.Response(400, json={"error": "strict not supported"})
        event = {"ids": [1], "keep": 1, "subject": "x", "region": "world", "already_sent": False}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"events": [event]})}}]})

    prov = OpenAICompatibleProvider(LLMConfig(provider="openai_compatible", base_url="https://llm.test/v1", api_key="k"))
    prov.http = httpx.AsyncClient(base_url="https://llm.test/v1", transport=httpx.MockTransport(handler))
    assert (await prov.generate("s", "u", DigestPlan)).events[0].keep == 1
    await prov.generate("s", "u", DigestPlan)
    assert len(bodies) == 3  # rejected once, then plain schema from then on


async def test_openai_compatible_reports_truncated_reply():
    import pytest

    from newsagg.llm import LLMError

    prov, _ = _provider([('{"events": [{"ids": [1', "length")])
    with pytest.raises(LLMError, match="max_tokens"):
        await prov.generate("s", "u", DigestPlan)


def test_split_counts_visible_text_not_markup():
    link = '💥 <b>Lead</b> text <a href="https://www.digi24.ro/stiri/externe/a-very-long-article-slug-1234567">anchor</a>'
    assert visible_length(link) == len("💥 Lead text anchor") + 1  # the emoji is two UTF-16 units
    assert len(split_blocks([link] * 40, limit=4096)) == 1


def test_typical_digest_fits_one_message():
    line = {"emoji": "🤝", "lead": "Premierul Luca Niculescu merge în Republica Moldova",
            "rest": "pe 27 octombrie, pentru discuții cu Maia Sandu și oficialii de la Chișinău despre proiectele comune",
            "anchor": "Maia Sandu"}
    url = "https://www.digi24.ro/stiri/actualitate/politica/premierul-luca-niculescu-merge-in-republica-moldova-3456789"
    lines = [render_line(line, url)] * 13  # 6 + 4 + 3
    parts = render_digest([(None, lines[:6]), ("::: ROMÂNIA", lines[6:10]), ("::: EXTERNE", lines[10:])], header="📰 <b>Știrile zilei</b>")
    assert len(parts) == 1
