"""Keyword labelling: used when the LLM is off, and as a fallback when an LLM call fails."""

from __future__ import annotations

import re

from .config import Config
from .textutil import strip_diacritics


def _compile(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    alts = "|".join(re.escape(strip_diacritics(w.lower())) for w in words)
    # Prefix match on word start, so "Moldov" also catches "Moldovei".
    return re.compile(rf"(?<!\w)(?:{alts})", re.UNICODE)


class KeywordClassifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.regions = [(rid, _compile(r.keywords)) for rid, r in cfg.regions.items()]
        self.topics = [(tid, _compile(t.keywords)) for tid, t in cfg.topics.items() if tid != "default"]
        self.block = _compile(cfg.filter.blocklist)

    @staticmethod
    def _text(title: str, summary: str | None) -> str:
        return strip_diacritics(f"{title} {(summary or '')[:300]}".lower())

    def is_blocked(self, title: str, summary: str | None = None) -> bool:
        return bool(self.block and self.block.search(self._text(title, summary)))

    def region(self, title: str, summary: str | None, default: str | None) -> str | None:
        text = self._text(title, summary)
        for rid, rx in self.regions:  # config order is priority order
            if rx and rx.search(text):
                return rid
        return default

    def topic(self, title: str, summary: str | None) -> str:
        text = self._text(title, summary)
        for tid, rx in self.topics:
            if rx and rx.search(text):
                return tid
        return "default"
