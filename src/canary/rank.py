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


def reasons(question: str, paper: Paper) -> dict:
    """Why this paper ranked here: relevance parts, never citations-as-relevance."""
    keys = keywords(question)
    title = paper.title.lower()
    abstract = (paper.abstract or "").lower()
    title_hits = sorted({k for k in keys if k in title})
    abstract_hits = sorted({k for k in keys if k in abstract})
    return {"keywords": keys, "title_hits": title_hits, "abstract_hits": abstract_hits,
            "citations": paper.citations,
            "citation_driven": not title_hits and not abstract_hits and paper.citations > 0}


def rerank(question: str, papers: list[Paper], top_n: int) -> list[Paper]:
    scored = [replace(p, score=score(question, p),
                      extra={**p.extra, "rank_reasons": reasons(question, p)})
              for p in papers]
    scored.sort(key=lambda p: p.score, reverse=True)
    return scored[:top_n]


def matched_terms(question: str, paper: Paper) -> set[str]:
    """Distinct question keywords engaging this paper's title or abstract."""
    keys = keywords(question)
    text = f"{paper.title}\n{paper.abstract or ''}".lower()
    return {k for k in keys if k in text}


def question_coverage(question: str, papers: list[Paper]) -> dict:
    """Does any candidate engage the question's breadth? Deterministic tripwire.

    Adequate iff one paper matches at least min(3, n_keywords) distinct terms.
    A weak verdict means no candidate covers the question — citation counts
    never override it. This flags retrieval failure; it cannot judge aboutness.
    """
    keys = keywords(question)
    if not keys:
        return {"keywords": [], "min_cover": 0, "best_cover": 0, "best_ref": None,
                "verdict": "adequate", "detail": "question has no keywords to cover"}
    if not papers:
        return {"keywords": keys, "min_cover": min(3, len(keys)), "best_cover": 0,
                "best_ref": None, "verdict": "weak", "detail": "no candidates retrieved"}
    best = max(papers, key=lambda p: len(matched_terms(question, p)))
    cover = len(matched_terms(question, best))
    need = min(3, len(keys))
    if cover >= need:
        return {"keywords": keys, "min_cover": need, "best_cover": cover,
                "best_ref": best.ref, "verdict": "adequate",
                "detail": f"{best.ref} covers {cover}/{len(keys)} question terms"}
    return {"keywords": keys, "min_cover": need, "best_cover": cover,
            "best_ref": best.ref, "verdict": "weak",
            "detail": f"best candidate {best.ref} covers {cover}/{len(keys)} question "
                      f"terms (needs {need}); citations do not establish relevance"}
