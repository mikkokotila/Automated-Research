"""Retrieval from OpenAlex and Semantic Scholar, merged and deduped."""

from __future__ import annotations

import os
import re

import httpx

from .papers import Paper
from .spec import ResearchSpec

OPENALEX_URL = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
MAILTO = "autoresearch@example.com"  # polite OpenAlex contact; override via client


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _openalex_abstract(inv_index: dict | None) -> str:
    if not inv_index:
        return ""
    try:
        pos: dict[int, str] = {}
        for word, idxs in inv_index.items():
            for i in idxs:
                pos[i] = word
        return " ".join(pos[i] for i in sorted(pos))
    except (TypeError, AttributeError):
        return ""


def openalex_search(spec: ResearchSpec, client: httpx.Client, limit: int = 25) -> list[Paper]:
    params: dict[str, str] = {
        "search": spec.question,
        "per-page": str(min(limit, 50)),
        "mailto": MAILTO,
    }
    if spec.year_from:
        params["filter"] = f"from_publication_date:{spec.year_from}-01-01"
    resp = client.get(OPENALEX_URL, params=params, timeout=30.0)
    resp.raise_for_status()
    out: list[Paper] = []
    for w in resp.json().get("results", []):
        doi = _clean((w.get("doi") or "").replace("https://doi.org/", ""))
        title = _clean(w.get("title"))
        if not title:
            continue
        authors = tuple(
            _clean(a.get("author", {}).get("display_name"))
            for a in w.get("authorships", [])[:10]
            if a.get("author", {}).get("display_name")
        )
        url = doi and f"https://doi.org/{doi}" or _clean(w.get("id"))
        out.append(
            Paper(
                ref=f"doi:{doi}" if doi else f"openalex:{w.get('id', '')}",
                title=title,
                abstract=_openalex_abstract(w.get("abstract_inverted_index")),
                authors=authors,
                year=w.get("publication_year"),
                venue=_clean(((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""),
                doi=doi,
                url=url,
                citations=int(w.get("cited_by_count") or 0),
                source="openalex",
            )
        )
    return out


def semscholar_search(spec: ResearchSpec, client: httpx.Client, limit: int = 25) -> list[Paper]:
    params = {
        "query": spec.question,
        "limit": str(min(limit, 50)),
        "fields": "title,abstract,authors,year,venue,doi,url,citationCount,externalIds",
    }
    if spec.year_from:
        params["year"] = f"{spec.year_from}-"
    headers = {}
    if os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
        headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
    resp = client.get(SEMANTIC_SCHOLAR_URL, params=params, headers=headers, timeout=30.0)
    resp.raise_for_status()
    out: list[Paper] = []
    for p in resp.json().get("data", []):
        title = _clean(p.get("title"))
        if not title:
            continue
        doi = _clean(p.get("doi") or "")
        authors = tuple(_clean(a.get("name")) for a in (p.get("authors") or [])[:10] if a.get("name"))
        url = _clean(p.get("url") or (doi and f"https://doi.org/{doi}")) or ""
        out.append(
            Paper(
                ref=f"doi:{doi.lower()}" if doi else f"s2:{p.get('paperId', '')}",
                title=title,
                abstract=_clean(p.get("abstract")),
                authors=authors,
                year=p.get("year"),
                venue=_clean(p.get("venue") or ""),
                doi=doi,
                url=url,
                citations=int(p.get("citationCount") or 0),
                source="semanticscholar",
            )
        )
    return out


def dedupe(papers: list[Paper]) -> list[Paper]:
    """Dedupe by DOI, else by normalized title. Keeps richer abstract."""
    seen: dict[str, Paper] = {}
    for p in papers:
        key = p.ref.lower() if p.ref.startswith("doi:") else "t:" + re.sub(r"\W+", "", p.title.lower())
        prev = seen.get(key)
        if prev is None or (not prev.abstract and p.abstract):
            seen[key] = p
    return list(seen.values())


def retrieve(spec: ResearchSpec, client: httpx.Client | None = None) -> list[Paper]:
    """Query both sources; degrade gracefully if one fails."""
    own = client is None
    client = client or httpx.Client()
    try:
        papers: list[Paper] = []
        errors: list[str] = []
        succeeded = 0
        for fn in (openalex_search, semscholar_search):
            try:
                papers.extend(fn(spec, client))
                succeeded += 1
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{fn.__name__}: {e}")
        if succeeded == 0:
            raise RuntimeError(f"retrieval failed: {'; '.join(errors)}")
        return dedupe(papers)
    finally:
        if own:
            client.close()
