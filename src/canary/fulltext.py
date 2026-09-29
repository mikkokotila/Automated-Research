"""Lazy full-text enrichment: broker PDF text for top-ranked arXiv papers.

The broker fetches, parses, and caches; this module only asks and attaches.
Every failure degrades to abstract-only loudly (counts + journal event),
never raises, and never mutates its inputs.
"""

from __future__ import annotations

import os
from dataclasses import replace

import httpx

from .journal import emit
from .papers import Paper

FULLTEXT_PROMPT_CHARS = 8000
FETCH_TIMEOUT_S = 120.0


def fetch(http: httpx.Client, arxiv_id: str) -> dict | None:
    """Broker full-text record for one arXiv id, or None without gate/failure."""
    gate = os.environ.get("CANARY_GATE_URL", "").rstrip("/")
    token = os.environ.get("CANARY_GATE_TOKEN", "")
    if not gate or not token or not arxiv_id:
        return None
    try:
        resp = http.get(f"{gate}/v1/sources/arxiv-pdf",
                        params={"id": arxiv_id},
                        headers={"Authorization": "Bearer " + token},
                        timeout=FETCH_TIMEOUT_S)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if (not isinstance(data, dict) or not isinstance(data.get("text"), str)
            or not data["text"]):
        return None
    return data


def enrich(papers: list[Paper], http: httpx.Client, max_n: int,
           journal=None) -> tuple[list[Paper], dict]:
    """Attach full text to the first max_n arXiv papers. Never raises."""
    out: list[Paper] = []
    ok = skipped = 0
    remaining = max(0, max_n)
    for p in papers:
        aid = p.identifiers.get("arxiv") if p.source == "arxiv" else ""
        if remaining > 0 and aid:
            try:
                rec = fetch(http, aid)
            except Exception:
                rec = None
            if rec is not None:
                out.append(replace(p, extra={**p.extra, "fulltext": rec["text"]},
                                   evidence="fulltext"))
                remaining -= 1
                ok += 1
                continue
            skipped += 1
        out.append(p)
    if max_n > 0:
        emit(journal, "review", "fulltext", f"enriched {ok}/{ok + skipped}")
    return out, {"ok": ok, "skipped": skipped}
