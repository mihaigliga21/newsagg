"""Text helpers: URL canonicalization, HTML stripping, title normalization."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "ref", "ref_src", "utm_id"}
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower(), path, urlencode(query), ""))


def url_hash(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def strip_diacritics(text: str) -> str:
    # Romanian cedilla forms (ş ţ) and comma forms (ș ț) both fold to s/t.
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_title(title: str) -> str:
    t = strip_diacritics(title.lower())
    t = _NON_WORD_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def first_sentence(text: str, max_len: int = 200) -> str:
    text = text.strip()
    m = re.search(r"(.+?[.!?…])(\s|$)", text)
    s = m.group(1) if m else text
    if len(s) > max_len:
        s = s[:max_len].rsplit(" ", 1)[0] + "…"
    return s


def truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[:max_len].rsplit(" ", 1)[0] + "…"
