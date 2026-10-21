from pathlib import Path

from newsagg.classify import KeywordClassifier
from newsagg.config import load_config
from newsagg.dedupe import find_match
from newsagg.render import render_digest, render_line, split_blocks, template_line
from newsagg.sources.rss import parse_feed
from newsagg.sources.telegram_web import parse_channel_page
from newsagg.textutil import canonical_url, normalize_title

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
