"""Local deterministic rerank: term overlap + citations + recency."""

from __future__ import annotations

import math
import re
from dataclasses import replace

from .papers import Paper

_STOP = frozenset(
    "a an the and or of in on to for with what are is how do does which who whom why when where that this those these from by as at it its be been were was have has had factors factor related relation relationship between among study studies".split()
)


def keywords(question: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", question.lower())
    return [w for w in words if w not in _STOP and len(w) > 2]


def score(question: str, paper: Paper, current_year: int = 2026) -> float:
    keys = keywords(question)
    if not keys:
        keys = re.findall(r"[a-z0-9]+", question.lower())
    title = paper.title.lower()
    abstract = (paper.abstract or "").lower()
    overlap = sum(2.0 for k in keys if k in title) + sum(1.0 for k in keys if k in abstract)
    norm = overlap / max(len(keys), 1)
    cite = math.log10(paper.citations + 1) / 3.0  # ~1.0 at 1000 cites
    recency = 0.0
    if paper.year:
        age = max(current_year - paper.year, 0)
        recency = max(0.0, 1.0 - age / 20.0) * 0.5
    abstract_bonus = 0.2 if paper.abstract else 0.0
    return round(norm + cite + recency + abstract_bonus, 4)


def rerank(question: str, papers: list[Paper], top_n: int) -> list[Paper]:
    scored = [replace(p, score=score(question, p)) for p in papers]
    scored.sort(key=lambda p: p.score, reverse=True)
    return scored[:top_n]
