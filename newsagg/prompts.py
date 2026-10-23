"""Prompts and output schemas for the LLM tasks: labelling items, writing digest lines, and picking
one line per event when the digest is built."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .config import Config
from .textutil import strip_media_tags


class ItemLabel(BaseModel):
    id: int
    is_news: bool = Field(description="false for ads, horoscopes, promos, quizzes, lifestyle filler")
    region: str = Field(description="one of the region ids, or 'other'")
    topic: str = Field(description="one of the topic ids")
    importance: int = Field(description="1 (trivial) to 5 (major national/international news)")


class ItemLabels(BaseModel):
    items: list[ItemLabel]


class DigestLine(BaseModel):
    id: int
    emoji: str
    lead: str = Field(description="the bold opening phrase: who/what, 2-8 words, a complete phrase")
    rest: str = Field(description="the rest of the sentence that follows the lead")
    anchor: str = Field(description="2-4 consecutive words copied exactly from `rest`, a whole phrase, to carry the link")


class DigestLines(BaseModel):
    lines: list[DigestLine]


def classify_system(cfg: Config) -> str:
    regions = "\n".join(f"- {rid}: {r.description or ', '.join(r.keywords[:8])}" for rid, r in cfg.regions.items())
    topics = ", ".join(t for t in cfg.topics if t != "default")
    return f"""You label news items for a Telegram news digest.

For each item decide:
- is_news: is it a real news report? Ads, horoscopes, promotions, quizzes, recipes and similar filler are not news.
- region: which region the story is mainly about. Use the region the events happen in or that is most affected, not the outlet's country. Region ids:
{regions}
- other: none of the above
- topic: one of: {topics}, default
- importance: 1 trivial, 2 minor, 3 normal, 4 important, 5 major (national or international significance).

Return one label per input item, with the same id."""


def classify_user(items: list[dict]) -> str:
    payload = [
        {"id": it["id"], "source": it["source_id"], "title": it["title"], "summary": (it.get("summary") or "")[:400]}
        for it in items
    ]
    return "Label these items:\n" + json.dumps(payload, ensure_ascii=False)


def write_system(cfg: Config) -> str:
    emojis = ", ".join(f"{t.emoji} {tid}" for tid, t in cfg.topics.items())
    lang = {"ro": "Romanian", "ru": "Russian", "en": "English"}.get(cfg.channel.language, cfg.channel.language)
    return f"""You write one-line headlines for a daily Telegram news digest, in {lang}.

Each story becomes exactly one sentence of at most about 200 characters, split into:
- emoji: one emoji that fits this particular story. Be specific: 🤝 talks, visits and agreements; 🗳 elections and votes; 🎤 statements and interviews; 🚔 police and arrests; ⚖️ courts; 🏗 construction; 🌉 bridges; 🔥 fires; 🎖 army; a country flag (🇺🇦 🇷🇺 🇺🇸 🇪🇺 🇷🇴 🇲🇩 …) for a story mainly about that country. The topic emojis ({emojis}) are only a fallback; use 🏛 only for acts of parliament or government as institutions, not for every story about a politician.
- lead: the opening phrase shown in bold, naming who did what (2-8 words). It must be a complete phrase that makes sense alone: never end it on a preposition, article or conjunction (la, de, în, pe, cu, din, al, a, un, o, și, că, să …) and never cut a quote in half. If a quote or a name carries the story, lead with it, followed by a colon when natural.
- rest: the rest of the sentence, continuing directly after the lead. If it starts with punctuation (a comma, a colon), start `rest` with that character.
- anchor: 2-4 consecutive words copied exactly from `rest`, somewhere in the middle, that will carry the link to the source. It must be a whole noun or verb phrase, not ending on a preposition, article or conjunction (good: "puncte de control", "fondurilor europene"; bad: "parcursul european al", "capul mesei la").

Rules: write in neutral, factual news style. Use only facts present in the provided titles and summaries; do not add numbers, names or claims that are not there. Leave out outlet labels such as VIDEO, FOTO, LIVE, (video), (foto), UPDATE. Use Romanian quotation marks „…" and correct diacritics (ș, ț). No hashtags, no trailing period needed. Return one line per input story, with the same id."""


def write_user(stories: list[dict]) -> str:
    payload = [
        {
            "id": s["id"],
            "topic": s.get("topic") or "default",
            "title": strip_media_tags(s["title"]),
            "summary": (s.get("summary") or "")[:600],
        }
        for s in stories
    ]
    return "Write lines for these stories:\n" + json.dumps(payload, ensure_ascii=False)


class DigestEvent(BaseModel):
    ids: list[int] = Field(description="ids of every candidate about this same event")
    keep: int = Field(description="the one id from `ids` whose line covers the event best")
    subject: str = Field(description="the main person or organisation, e.g. 'Luca Niculescu', 'Vladimir Putin'")
    region: str = Field(description="region id where the event happens or that it is about")
    already_sent: bool = Field(description="true if a line in `already_sent` covers this event and there is no real new development")


class DigestPlan(BaseModel):
    events: list[DigestEvent]


def curate_system(cfg: Config) -> str:
    regions = "\n".join(f"- {rid}: {r.description or rid}" for rid, r in cfg.regions.items())
    return f"""You edit a daily Telegram news digest. You get candidate lines, ranked best first within each section, and the lines already sent in recent digests.

1. Group the candidates by real-world event. Be thorough: the same vote, meeting, decision, statement, attack or incident is ONE event even when outlets word it very differently or stress different details. Examples of one event each:
   - "Parlamentul a adoptat politica fiscală pentru 2027" / "Deputații PAS au votat noua politică fiscală" / "Politica fiscală pentru 2027, votată în lectură finală"
   - "Prima ședință CSAT cu miniștrii PSD, convocată de Nicușor Dan" / "CSAT se reunește în noul format"
   - "Hamas amenință Israelul cu un nou atac" / "Hamas amenință Israelul cu noi atacuri, similare masacrului din 7 octombrie"
   - "Putin acceptă să reia negocierile de pace" / "Kremlinul: negocierile se reiau luna viitoare"
   Lines that only share a person but report different happenings are different events.
2. For each event decide `already_sent`: true when a line in `already_sent` reports the same event, unless the candidate brings a real new development (a vote passed after a debate, a death toll that changed, a new decision). Rewording or extra detail is not a development.
3. Decide the event's `region` from what it is about, not from the outlet: a Moldovan law reported by a Romanian site is moldova. Region ids:
{regions}

For each event return:
- ids: every candidate id about it (each candidate belongs to exactly one event)
- keep: the id whose line is most informative and specific; prefer higher ranked ids when they are equally good
- subject: the main person or organisation, written the same way every time it recurs (always "Luca Niculescu", never sometimes "Premierul Niculescu")
- region: one of the region ids above
- already_sent: as above

Every candidate id must appear in exactly one event. Do not rewrite the lines."""


def curate_user(candidates: list[dict], already_sent: list[str] | None = None) -> str:
    payload = {
        "candidates": [
            {"id": c["id"], "section": c["section"], "title": c.get("title", ""), "line": c["text"]}
            for c in candidates
        ],
        "already_sent": already_sent or [],
    }
    return "Group these candidates:\n" + json.dumps(payload, ensure_ascii=False)
