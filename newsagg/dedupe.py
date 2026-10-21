"""Finds the existing story a new item belongs to, by fuzzy title match."""

from __future__ import annotations

from rapidfuzz import fuzz

from .textutil import normalize_title

# Very common words carry no signal when comparing two Romanian headlines.
_STOP = set(
    "si sa se in la de din pe cu a al ai ale o un una unei unui ca care ce mai nu este sunt "
    "pentru dupa despre prin sau iar fost fi va vor".split()
)


def _tokens(norm: str) -> list[str]:
    return [t for t in norm.split() if t not in _STOP]


def similarity(a_norm: str, b_norm: str) -> float:
    a, b = _tokens(a_norm), _tokens(b_norm)
    if not a or not b:
        return 0.0
    sa, sb = " ".join(a), " ".join(b)
    sort_score = fuzz.token_sort_ratio(sa, sb)
    # token_set_ratio is 100 whenever one title's words are a subset of the other's,
    # so only trust it when both titles have a similar number of words.
    ratio = min(len(a), len(b)) / max(len(a), len(b))
    set_score = fuzz.token_set_ratio(sa, sb) if ratio >= 0.6 else 0
    return max(sort_score, set_score * 0.95)


def find_match(title: str, candidates: list[tuple[int, str]], threshold: int, min_tokens: int) -> int | None:
    """candidates: [(story_id, norm_title)]. Returns the best matching story id or None."""
    norm = normalize_title(title)
    if len(_tokens(norm)) < min_tokens:
        return None
    best_id, best = None, 0.0
    for sid, cand in candidates:
        score = similarity(norm, cand)
        if score > best:
            best_id, best = sid, score
    return best_id if best >= threshold else None
