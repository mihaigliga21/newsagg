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


def _story(sid, section):
    return {"id": sid, "section": section}


def test_apply_plan_one_line_per_event_and_subject_cap():
    ranked = {"ro": [_story(1, "ro"), _story(2, "ro"), _story(3, "ro"), _story(4, "ro")],
              "ext": [_story(5, "ext"), _story(6, "ext"), _story(7, "ext")]}
    scores = {1: 50, 2: 49, 3: 48, 4: 10, 5: 40, 6: 39, 7: 20}
    plan = DigestPlan(events=[
        DigestEvent(ids=[1], keep=1, subject="Luca Niculescu"),
        DigestEvent(ids=[2], keep=2, subject=" LUCA  Niculescu"),  # same subject after normalizing
        DigestEvent(ids=[3], keep=3, subject="luca niculescu"),
        DigestEvent(ids=[5, 6, 99], keep=6, subject="Vladimir Putin"),  # 99 is unknown and ignored
    ])
    chosen, folded = apply_plan(ranked, plan, {"ro": 3, "ext": 3}, 2, scores)
    assert [s["id"] for s in chosen["ro"]] == [1, 2, 4]  # 3 is a third Niculescu line
    assert [s["id"] for s in chosen["ext"]] == [6, 7]
    assert folded == [5]


def test_apply_plan_without_plan_is_plain_ranking():
    ranked = {"ro": [_story(1, "ro"), _story(2, "ro"), _story(3, "ro")]}
    chosen, folded = apply_plan(ranked, None, {"ro": 2}, 2, {1: 3, 2: 2, 3: 1})
    assert [s["id"] for s in chosen["ro"]] == [1, 2] and folded == []


def test_apply_plan_bad_keep_falls_back_to_best_ranked():
    ranked = {"ro": [_story(1, "ro"), _story(2, "ro")]}
    plan = DigestPlan(events=[DigestEvent(ids=[2, 1], keep=42, subject="x"), DigestEvent(ids=[1], keep=1, subject="y")])
    chosen, folded = apply_plan(ranked, plan, {"ro": 5}, 2, {1: 2, 2: 1})
    assert [s["id"] for s in chosen["ro"]] == [1] and folded == [2]


async def test_openai_compatible_returns_digest_plan():
    import json

    import httpx

    from newsagg.config import LLMConfig
    from newsagg.llm.openai_compatible import OpenAICompatibleProvider

    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        content = json.dumps({"events": [{"ids": [1, 2], "keep": 2, "subject": "Vladimir Putin"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    prov = OpenAICompatibleProvider(LLMConfig(provider="openai_compatible", base_url="https://llm.test/v1", api_key="k"))
    prov.http = httpx.AsyncClient(base_url="https://llm.test/v1", transport=httpx.MockTransport(handler))
    plan = await prov.generate("s", "u", DigestPlan)
    assert plan.events[0].keep == 2
    assert sent["response_format"]["json_schema"]["name"] == "DigestPlan"


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
