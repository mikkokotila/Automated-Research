"""Retrieval from OpenAlex and arXiv, merged and deduped."""

from __future__ import annotations

import hashlib
import os
import re
import time

import httpx

from .papers import Paper
from .spec import ResearchSpec

OPENALEX_URL = "https://api.openalex.org/works"
ARXIV_URL = "https://export.arxiv.org/api/query"
# Brokered path: guests resolve each provider URL to a broker route. arXiv is
# keyless and quota-free, so it carries the run when aggregators rate-limit.
_GATE_SOURCES = {
    OPENALEX_URL: "openalex",
    ARXIV_URL: "arxiv",
}
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
    if gate and url in _GATE_SOURCES:
        url = gate.rstrip("/") + "/v1/sources/" + _GATE_SOURCES[url]
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


# Per-provider ceilings: pages of 50, at most 8 pages (400 papers). Hundreds-
# paper runs page; small runs behave exactly as before (single page).
_PER_PAGE = 50
_MAX_PAGES = 8


def openalex_search(spec: ResearchSpec, client: httpx.Client, limit: int = 25,
                    capture: dict | None = None) -> list[Paper]:
    base: dict[str, str] = {"search": search_text(spec.question)}
    api_key = os.environ.get("OPENALEX_API_KEY", "")
    if api_key:
        base["api_key"] = api_key
    if spec.year_from:
        base["filter"] = f"from_publication_date:{spec.year_from}-01-01"
    started = time.monotonic()
    bodies: list[bytes] = []
    pages = 0
    out: list[Paper] = []
    want = min(max(limit, 1), _PER_PAGE * _MAX_PAGES)
    while len(out) < want and pages < _MAX_PAGES:
        params = dict(base, page=str(pages + 1),
                      **{"per-page": str(min(_PER_PAGE, want - len(out)))})
        resp = _get(client, OPENALEX_URL, params)
        bodies.append(resp.content)
        pages += 1
        body = resp.json()
        results = body.get("results", []) if isinstance(body, dict) else []
        if not results:
            break
        page_hash = "sha256:" + hashlib.sha256(bodies[-1]).hexdigest()
        for w in results:
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
                    extra={"abstract_complete": complete, "cache_hash": page_hash,
                           "oa_status": _clean((w.get("open_access") or {}).get("oa_status") or "")},
                )
            )
            if len(out) >= want:
                break
        if len(results) < int(params["per-page"]):
            break  # short page: no more results
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    cache_hash = "sha256:" + hashlib.sha256(b"".join(bodies)).hexdigest()
    if capture is not None:
        capture.update({"outcome": "ok", "papers": len(out), "latency_ms": latency_ms,
                        "cache_hash": cache_hash, "pages": pages})
    return out


def arxiv_query(question: str, max_terms: int = 6) -> str:
    """Distinctive keywords as a conjunctive arXiv query.

    Bare space-separated terms default to OR on the API, which drifts into
    unrelated work; explicit AND keeps every term binding. Terms come from
    the same keyword machinery as ranking, so the query can never drift
    outside the question's own vocabulary.
    """
    from .rank import keywords

    # No fallback: when the question has no keywords the caller skips the
    # call, which beats querying stopwords that match the whole archive.
    terms = keywords(question)[:max_terms]
    return " AND ".join(f"all:{t}" for t in terms)


def arxiv_search(spec: ResearchSpec, client: httpx.Client, limit: int = 25,
                 capture: dict | None = None) -> list[Paper]:
    """Search arXiv via the broker's parsed-JSON shape (``{"entries": [...]}``).

    Foreign shapes (other providers' payloads, direct Atom XML) yield no
    papers rather than a crash: source confusion degrades, never poisons.
    arXiv has no server-side year filter, so ``year_from`` is applied here;
    papers with an unknown year are kept. When the full conjunction finds
    nothing, one relaxed query (3 longest question terms, still AND) runs
    before giving up; the fallback stays inside the question's vocabulary.
    """
    from .rank import keywords

    query = arxiv_query(spec.question)
    if not query:
        if capture is not None:
            capture.update({"outcome": "ok", "papers": 0, "latency_ms": 0.0,
                            "cache_hash": None})
        return []
    queries = [query]
    terms = keywords(spec.question)
    if len(terms) > 3:
        short = sorted(set(terms), key=lambda t: (-len(t), t))[:3]
        relaxed = " AND ".join(f"all:{t}" for t in short)
        if relaxed != query:
            queries.append(relaxed)
    started = time.monotonic()
    bodies: list[bytes] = []
    pages = 0
    relaxed_used = False
    out: list[Paper] = []
    want = min(max(limit, 1), _PER_PAGE * _MAX_PAGES)
    for qi, query in enumerate(queries):
        if qi > 0:
            relaxed_used = True
        consumed = 0  # results seen (kept or year-filtered); the start offset
        round_papers = len(out)
        while len(out) < want and pages < _MAX_PAGES:
            params = {
                "search_query": query,
                "start": str(consumed),
                "max_results": str(min(_PER_PAGE, want - len(out))),
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
            resp = _get(client, ARXIV_URL, params)
            bodies.append(resp.content)
            pages += 1
            try:
                body = resp.json()
            except ValueError:
                body = None  # direct Atom XML or any non-JSON body: no papers
            entries = body.get("entries", []) if isinstance(body, dict) else []
            if not isinstance(entries, list):
                entries = []
            if not entries:
                break
            consumed += len(entries)
            page_hash = "sha256:" + hashlib.sha256(bodies[-1]).hexdigest()
            for e in entries:
                if not isinstance(e, dict):
                    continue
                title = _clean(e.get("title"))
                aid = _clean(e.get("id"))
                if not title or not aid:
                    continue
                year = e.get("year")
                year = year if isinstance(year, int) else None
                if spec.year_from and year is not None and year < spec.year_from:
                    continue
                authors = e.get("authors") or []
                if not isinstance(authors, list):
                    authors = []
                authors = tuple(a for a in (_clean(a) for a in authors[:10]) if a)
                categories = e.get("categories") or []
                if not isinstance(categories, list):
                    categories = []
                categories = [_clean(c) for c in categories if _clean(c)]
                doi = normalize_doi(e.get("doi"))
                url = _clean(e.get("url_abs")) or (f"https://doi.org/{doi}" if doi else "")
                oa_url = _clean(e.get("url_pdf"))
                abstract = _clean(e.get("abstract"))
                out.append(
                    Paper(
                        ref=f"doi:{doi}" if doi else f"arxiv:{aid}",
                        title=title,
                        abstract=abstract,
                        authors=authors,
                        year=year,
                        venue="arXiv",
                        doi=doi,
                        url=url,
                        citations=0,  # arXiv reports no citation counts; never fabricate one
                        source="arxiv",
                        evidence=_evidence_tier(abstract, oa_url),
                        oa_url=oa_url,
                        license="",
                        identifiers={"arxiv": aid},
                        extra={"version": _clean(e.get("version")), "categories": categories,
                               "primary_category": _clean(e.get("primary_category")),
                               "cache_hash": page_hash},
                    )
                )
                if len(out) >= want:
                    break
            if len(entries) < int(params["max_results"]):
                break  # short page: no more results
        if len(out) > round_papers:
            break  # this query produced; never dilute with the relaxed one
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    cache_hash = "sha256:" + hashlib.sha256(b"".join(bodies)).hexdigest()
    if capture is not None:
        capture.update({"outcome": "ok", "papers": len(out), "latency_ms": latency_ms,
                        "cache_hash": cache_hash, "pages": pages,
                        "query_relaxed": relaxed_used})
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
    # Each provider covers the full pool: small runs keep the legacy 25/page,
    # hundreds-paper runs page up to the per-provider ceiling (400).
    per_provider = min(_PER_PAGE * _MAX_PAGES, max(25, spec.max_papers))
    for name, fn in (("openalex", openalex_search), ("arxiv", arxiv_search)):
        capture: dict = {"outcome": "error", "papers": 0, "latency_ms": 0.0,
                         "cache_hash": None, "error": None}
        started = time.monotonic()
        try:
            papers.extend(fn(spec, client, per_provider, capture))
            providers[name] = capture  # fn filled outcome/latency/hash on success
        except (httpx.HTTPError, ValueError) as e:
            capture["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            capture["error"] = f"{type(e).__name__}: {e}"[:300]
            providers[name] = capture
    return papers, providers


def retrieve_with_report(spec: ResearchSpec,
                         client: httpx.Client | None = None) -> tuple[list[Paper], dict]:
    """Query all sources with a full provenance report; refine once if empty."""
    from .rank import question_coverage

    own = client is None
    client = client or httpx.Client()
    try:
        queries = [spec.question]
        papers, providers = _attempt(spec, client)
        refinements: list[dict] = []
        if not papers and all(p["outcome"] == "ok" for p in providers.values()):
            # All providers answered but found nothing: one simplification retry.
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
    """Query all sources; degrade gracefully if some fail."""
    return retrieve_with_report(spec, client)[0]
