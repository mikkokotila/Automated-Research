"""Lazy full-text enrichment: broker PDF text for top-ranked arXiv papers.

The broker fetches, parses, and caches; this module only asks and attaches.
Every failure degrades to abstract-only loudly (counts + journal event),
never raises, and never mutates its inputs.
"""

from __future__ import annotations

import concurrent.futures
import os
import time
from dataclasses import replace

import httpx

from .journal import emit
from .papers import Paper

FULLTEXT_PROMPT_CHARS = 8000
FETCH_TIMEOUT_S = 120.0
# Concurrent broker PDF fetches. Cache hits return at once; cold misses still
# serialize behind the broker's arXiv courtesy spacing.
_FETCH_WORKERS = 4


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
    """Attach full text to the first max_n arXiv papers. Never raises.

    Candidates fetch in small concurrent chunks, in paper order; the quota
    fills from the earliest successes, exactly as the serial version did.
    """
    started = time.monotonic()
    quota = max(0, max_n)
    targets = [i for i, p in enumerate(papers)
               if quota > 0 and p.source == "arxiv" and p.identifiers.get("arxiv")]
    records: dict[int, dict | None] = {}

    def one(i: int) -> tuple[int, dict | None]:
        try:
            return i, fetch(http, papers[i].identifiers.get("arxiv", ""))
        except Exception:
            return i, None

    attach: dict[int, str] = {}
    ok = skipped = 0
    for chunk in (targets[i:i + _FETCH_WORKERS]
                  for i in range(0, len(targets), _FETCH_WORKERS)):
        if quota <= 0:
            break
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, len(chunk))) as ex:
            for i, rec in ex.map(one, chunk):
                records[i] = rec
        # Consume this chunk in paper order before fetching further: later
        # papers are never fetched once the quota fills from earlier ones,
        # and fetched-but-unneeded records count as nothing at all.
        for i in chunk:
            if quota <= 0:
                break
            rec = records.get(i)
            if rec is not None:
                attach[i] = rec["text"]
                quota -= 1
                ok += 1
            else:
                skipped += 1
    out: list[Paper] = []
    for i, p in enumerate(papers):
        if i in attach:
            out.append(replace(p, extra={**p.extra, "fulltext": attach[i]},
                               evidence="fulltext"))
        else:
            out.append(p)
    elapsed = time.monotonic() - started
    if max_n > 0:
        emit(journal, "review", "fulltext",
             f"enriched {ok}/{ok + skipped} in {elapsed:.1f}s")
    return out, {"ok": ok, "skipped": skipped}
