"""Retrieval from OpenAlex and Semantic Scholar, merged and deduped."""

from __future__ import annotations

import hashlib
import os
import re
import time

import httpx

from .papers import Paper
from .spec import ResearchSpec

OPENALEX_URL = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
# OpenAlex retired the mailto polite pool; auth is a free API key (see
# docs/RETRIEVAL_CONTRACTS.md). Without one, requests run on the keyless budget.
MAX_ATTEMPTS = 3
BACKOFF_S = (1.0, 3.0)
RETRY_AFTER_CAP_S = 10.0


def _wait(attempt: int, resp: httpx.Response | None = None) -> None:
    """Bounded backoff; honors Retry-After only within the cap."""
    wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
    if resp is not None:
        try:
            hint = float(resp.headers.get("retry-after", ""))
            if 0 <= hint <= RETRY_AFTER_CAP_S:
                wait = hint
        except ValueError:
            pass
    time.sleep(wait)


def _get(client: httpx.Client, url: str, params: dict, headers: dict | None = None) -> httpx.Response:
    """GET with retries on transient failures (429/5xx, timeouts)."""
    gate = os.environ.get("CANARY_GATE_URL")
    if gate and url in (OPENALEX_URL, SEMANTIC_SCHOLAR_URL):
        name = "openalex" if url == OPENALEX_URL else "semanticscholar"
        url = gate.rstrip("/") + "/v1/sources/" + name
        headers = {"Authorization": "Bearer " + os.environ.get("CANARY_GATE_TOKEN", "")}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = client.get(url, params=params, headers=headers, timeout=30.0)
            if (resp.status_code == 429 or 500 <= resp.status_code <= 599) and attempt < MAX_ATTEMPTS - 1:
                last = httpx.HTTPStatusError(f"transient {resp.status_code}", request=resp.request, response=resp)
                _wait(attempt, resp)
                continue
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            last = e
            code = e.response.status_code if e.response is not None else 0
            if (code == 429 or 500 <= code <= 599) and attempt < MAX_ATTEMPTS - 1:
                _wait(attempt, e.response)
                continue
            raise
        except httpx.TimeoutException as e:
            last = e
            if attempt < MAX_ATTEMPTS - 1:
                _wait(attempt)
                continue
            raise
    raise RuntimeError(f"GET {url} failed after {MAX_ATTEMPTS} attempts: {last}")


def _clean(text: str | None) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):  # providers ship ints where strings belong
        text = str(text)
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def normalize_doi(raw: str | None) -> str:
    """DOI to a canonical key: bare, lowercase, validated. "" when absent."""
    if not raw or not isinstance(raw, str):
        return ""
    doi = raw.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break
    doi = doi.strip().rstrip(".")
    if not re.fullmatch(r"10\.\d+/\S+", doi):
        return ""
    return doi


def search_text(question: str) -> str:
    """Strip characters that break source query syntax (e.g. OpenAlex 400s on '?')."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s\-':]", "", question)).strip()


def _openalex_abstract_with_gaps(inv_index: dict | None) -> tuple[str, bool]:
    """Reconstructed abstract plus completeness: gaps mean truncation."""
    if not inv_index:
        return "", True
    try:
        pos: dict[int, str] = {}
        total = 0
        for word, idxs in inv_index.items():
            for i in idxs:
                pos[i] = word
                total += 1
        if not pos:
            return "", True
        complete = len(pos) == total and set(pos) == set(range(len(pos)))
        return " ".join(pos[i] for i in sorted(pos)), complete
    except (TypeError, AttributeError):
        return "", False


def _openalex_abstract(inv_index: dict | None) -> str:
    return _openalex_abstract_with_gaps(inv_index)[0]


def _evidence_tier(abstract: str, oa_url: str) -> str:
    if oa_url:
        return "fulltext"
    return "abstract" if abstract else "none"


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def openalex_search(spec: ResearchSpec, client: httpx.Client, limit: int = 25,
                    capture: dict | None = None) -> list[Paper]:
    params: dict[str, str] = {
        "search": search_text(spec.question),
        "per-page": str(min(limit, 50)),
    }
    api_key = os.environ.get("OPENALEX_API_KEY", "")
    if api_key:
        params["api_key"] = api_key
    if spec.year_from:
        params["filter"] = f"from_publication_date:{spec.year_from}-01-01"
    started = time.monotonic()
    resp = _get(client, OPENALEX_URL, params)
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    body = resp.json()
    cache_hash = "sha256:" + hashlib.sha256(resp.content).hexdigest()
    out: list[Paper] = []
    for w in body.get("results", []) if isinstance(body, dict) else []:
        if not isinstance(w, dict):
            continue
        ids = w.get("ids") or {}
        doi = normalize_doi(w.get("doi") or ids.get("doi"))
        title = _clean(w.get("title"))
        if not title:
            continue
        authors = tuple(
            _clean(((a or {}).get("author") or {}).get("display_name"))
            for a in (w.get("authorships") or [])[:10]
            if ((a or {}).get("author") or {}).get("display_name")
        )
        url = (doi and f"https://doi.org/{doi}") or _clean(w.get("id"))
        abstract, complete = _openalex_abstract_with_gaps(w.get("abstract_inverted_index"))
        best_oa = w.get("best_oa_location") or {}
        oa_url = _clean(best_oa.get("pdf_url") or best_oa.get("landing_page_url") or "")
        license = _clean(best_oa.get("license") or "")
        identifiers = {"openalex": _clean(w.get("id"))}
        for key in ("doi", "mag", "pmid", "pmcid"):
            if ids.get(key):
                identifiers[key] = _clean(ids.get(key))
        out.append(
            Paper(
                ref=f"doi:{doi}" if doi else f"openalex:{w.get('id', '')}",
                title=title,
                abstract=abstract,
                authors=authors,
                year=w.get("publication_year"),
                venue=_clean(((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""),
                doi=doi,
                url=url,
                citations=_as_int(w.get("cited_by_count")),
                source="openalex",
                evidence=_evidence_tier(abstract, oa_url),
                oa_url=oa_url,
                license=license,
                identifiers=identifiers,
                extra={"abstract_complete": complete, "cache_hash": cache_hash,
                       "oa_status": _clean((w.get("open_access") or {}).get("oa_status") or "")},
            )
        )
    if capture is not None:
        capture.update({"outcome": "ok", "papers": len(out), "latency_ms": latency_ms,
                        "cache_hash": cache_hash})
    return out


S2_FIELDS = ("title,abstract,authors,year,venue,url,citationCount,externalIds,"
             "openAccessPdf,publicationTypes")


def semscholar_search(spec: ResearchSpec, client: httpx.Client, limit: int = 25,
                      capture: dict | None = None) -> list[Paper]:
    params = {
        "query": search_text(spec.question),
        "limit": str(min(limit, 50)),
        "fields": S2_FIELDS,
    }
    if spec.year_from:
        params["year"] = f"{spec.year_from}-"
    headers = {}
    if os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
        headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
    started = time.monotonic()
    resp = _get(client, SEMANTIC_SCHOLAR_URL, params, headers)
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    body = resp.json()
    cache_hash = "sha256:" + hashlib.sha256(resp.content).hexdigest()
    out: list[Paper] = []
    for p in body.get("data", []) if isinstance(body, dict) else []:
        if not isinstance(p, dict):
            continue
        title = _clean(p.get("title"))
        if not title:
            continue
        external = p.get("externalIds") or {}
        if not isinstance(external, dict):
            external = {}
        # The graph API exposes DOI under externalIds, not as a top-level field;
        # a top-level value is honored only as a forward-compatible fallback.
        doi = normalize_doi(external.get("DOI") or external.get("doi") or p.get("doi"))
        authors = tuple(_clean((a or {}).get("name")) for a in (p.get("authors") or [])[:10]
                        if (a or {}).get("name"))
        url = _clean(p.get("url") or (f"https://doi.org/{doi}" if doi else "")) or ""
        oa = p.get("openAccessPdf") or {}
        oa_url = _clean(oa.get("url") or "")
        abstract = _clean(p.get("abstract"))
        identifiers = {"s2": _clean(p.get("paperId"))}
        for key, value in external.items():
            if value:
                identifiers[str(key).lower()] = _clean(value)
        out.append(
            Paper(
                ref=f"doi:{doi}" if doi else f"s2:{p.get('paperId', '')}",
                title=title,
                abstract=abstract,
                authors=authors,
                year=p.get("year"),
                venue=_clean(p.get("venue") or ""),
                doi=doi,
                url=url,
                citations=_as_int(p.get("citationCount")),
                source="semanticscholar",
                evidence=_evidence_tier(abstract, oa_url),
                oa_url=oa_url,
                license=_clean(oa.get("license") or ""),
                identifiers=identifiers,
                extra={"publication_types": p.get("publicationTypes") or [],
                       "cache_hash": cache_hash},
            )
        )
    if capture is not None:
        capture.update({"outcome": "ok", "papers": len(out), "latency_ms": latency_ms,
                        "cache_hash": cache_hash})
    return out


def dedupe_with_stats(papers: list[Paper]) -> tuple[list[Paper], dict]:
    """Dedupe by DOI, else by normalized title. Keeps richer abstract."""
    seen: dict[str, Paper] = {}
    merged = 0
    for p in papers:
        key = (f"doi:{p.doi}" if p.doi
               else "t:" + re.sub(r"\W+", "", p.title.lower()))
        prev = seen.get(key)
        if prev is None:
            seen[key] = p
        else:
            merged += 1
            if not prev.abstract and p.abstract:
                seen[key] = p
    return list(seen.values()), {"candidates": len(papers), "kept": len(seen),
                                 "merged": merged}


def dedupe(papers: list[Paper]) -> list[Paper]:
    """Dedupe by DOI, else by normalized title. Keeps richer abstract."""
    return dedupe_with_stats(papers)[0]


def refined_query(question: str) -> str:
    """One bounded refinement: distinctive keywords only, never new terms.

    Simplification cannot drift into unrelated research: every refinement term
    already appears in the original question.
    """
    from .rank import keywords

    words = keywords(question)[:6]
    return " ".join(words) if words else search_text(question)


def _attempt(spec: ResearchSpec, client: httpx.Client) -> tuple[list[Paper], dict]:
    providers: dict[str, dict] = {}
    papers: list[Paper] = []
    for name, fn in (("openalex", openalex_search), ("semanticscholar", semscholar_search)):
        capture: dict = {"outcome": "error", "papers": 0, "latency_ms": 0.0,
                         "cache_hash": None, "error": None}
        started = time.monotonic()
        try:
            papers.extend(fn(spec, client, 25, capture))
            providers[name] = capture  # fn filled outcome/latency/hash on success
        except (httpx.HTTPError, ValueError) as e:
            capture["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            capture["error"] = f"{type(e).__name__}: {e}"[:300]
            providers[name] = capture
    return papers, providers


def retrieve_with_report(spec: ResearchSpec,
                         client: httpx.Client | None = None) -> tuple[list[Paper], dict]:
    """Query both sources with a full provenance report; refine once if empty."""
    from .rank import question_coverage

    own = client is None
    client = client or httpx.Client()
    try:
        queries = [spec.question]
        papers, providers = _attempt(spec, client)
        refinements: list[dict] = []
        if not papers and all(p["outcome"] == "ok" for p in providers.values()):
            # Both providers answered but found nothing: one simplification retry.
            simplified = refined_query(spec.question)
            if simplified and simplified != search_text(spec.question):
                refinements.append({"from": spec.question, "to": simplified})
                queries.append(simplified)
                rspec = ResearchSpec(question=simplified, max_papers=spec.max_papers,
                                     year_from=spec.year_from)
                papers, providers = _attempt(rspec, client)
        if all(p["outcome"] != "ok" for p in providers.values()):
            errors = "; ".join(f"{n}: {p['error']}" for n, p in providers.items())
            raise RuntimeError(f"retrieval failed: {errors}")
        unique, dedupe_stats = dedupe_with_stats(papers)
        coverage = question_coverage(spec.question, unique)
        failed = [n for n, p in providers.items() if p["outcome"] != "ok"]
        if failed:
            warning = (f"degraded coverage: {', '.join(failed)} failed; "
                       f"{len(unique)} papers from remaining sources only")
        elif coverage["verdict"] == "weak":
            warning = f"weak relevance: {coverage['detail']}"
        else:
            warning = None
        return unique, {"queries": queries, "providers": providers,
                        "refinements": refinements, "dedupe": dedupe_stats,
                        "coverage": coverage, "warning": warning}
    finally:
        if own:
            client.close()


def retrieve(spec: ResearchSpec, client: httpx.Client | None = None) -> list[Paper]:
    """Query both sources; degrade gracefully if one fails."""
    return retrieve_with_report(spec, client)[0]
