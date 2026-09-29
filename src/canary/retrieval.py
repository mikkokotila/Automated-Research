"""Retrieval from OpenAlex and arXiv, merged and deduped."""

from __future__ import annotations

import concurrent.futures
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


# Per-provider ceilings: pages of 50, up to 500 pages (25k papers). Pages
# after the first fetch concurrently when the provider reports a result
# total; small runs behave exactly as before (single page, no workers).
_PER_PAGE = 50
_MAX_PAGES = 500
# Concurrent page fetches per provider. OpenAlex absorbs this; arXiv's
# broker courtesy spacing still serializes upstream, so its share stays slow.
_PAGE_WORKERS = 8


def _openalex_works(results: list, page_hash: str) -> list[Paper]:
    """Parse one OpenAlex result page. Skips shapeless works, never raises."""
    out: list[Paper] = []
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
    return out


def _openalex_results(body) -> tuple[list, int | None]:
    """Result list plus advertised total (None when the shape hides it)."""
    results = body.get("results", []) if isinstance(body, dict) else []
    if not isinstance(results, list):
        results = []
    total: int | None = None
    meta = body.get("meta") if isinstance(body, dict) else None
    if isinstance(meta, dict) and isinstance(meta.get("count"), int):
        total = meta["count"]
    return results, total


def _openalex_page(client: httpx.Client, base: dict, page: int,
                   per_page: int) -> tuple[bytes, list, int | None]:
    params = dict(base, page=str(page), **{"per-page": str(per_page)})
    resp = _get(client, OPENALEX_URL, params)
    results, total = _openalex_results(resp.json())
    return resp.content, results, total


def openalex_search(spec: ResearchSpec, client: httpx.Client, limit: int = 25,
                    capture: dict | None = None) -> list[Paper]:
    base: dict[str, str] = {"search": search_text(spec.question)}
    api_key = os.environ.get("OPENALEX_API_KEY", "")
    if api_key:
        base["api_key"] = api_key
    if spec.year_from:
        base["filter"] = f"from_publication_date:{spec.year_from}-01-01"
    started = time.monotonic()
    want = min(max(limit, 1), _PER_PAGE * _MAX_PAGES)
    # Page 1 stays sequential: a first-page failure fails the provider
    # (existing degraded-coverage semantics), and a short page ends small
    # pools without spawning workers.
    raw, results, total = _openalex_page(
        client, base, 1, min(_PER_PAGE, want))
    fetched: list[tuple[int, bytes, list]] = [(1, raw, results)]
    truncated: str | None = None
    if results and len(results) >= min(_PER_PAGE, want) and len(results) < want:
        if total is not None and total > len(results):
            last = min((min(total, want) + _PER_PAGE - 1) // _PER_PAGE,
                       _MAX_PAGES)
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, min(_PAGE_WORKERS, last - 1))) as ex:
                futs = {ex.submit(_openalex_page, client, base, p, _PER_PAGE): p
                        for p in range(2, last + 1)}
                done: dict[int, tuple[bytes, list]] = {}
                for fut in concurrent.futures.as_completed(futs):
                    try:
                        fraw, fresults, _ = fut.result()
                    except (httpx.HTTPError, ValueError) as e:
                        truncated = f"{type(e).__name__}: {e}"[:200]
                        for f in futs:
                            f.cancel()
                        break
                    done[futs[fut]] = (fraw, fresults)
            for p in range(2, last + 1):
                if p not in done:
                    break  # failure or cancel: keep the ordered prefix
                fraw, fresults = done[p]
                fetched.append((p, fraw, fresults))
                if len(fresults) < _PER_PAGE:
                    break  # stale count: a short page still ends the pool
        else:
            # No advertised total: legacy sequential paging, same requests.
            page = 2
            have = len(results)
            while have < want and page <= _MAX_PAGES:
                asked = min(_PER_PAGE, want - have)
                try:
                    fraw, fresults, _ = _openalex_page(client, base, page, asked)
                except (httpx.HTTPError, ValueError) as e:
                    truncated = f"{type(e).__name__}: {e}"[:200]
                    break
                fetched.append((page, fraw, fresults))
                have += len(fresults)
                page += 1
                if len(fresults) < asked:
                    break  # short page: no more results
    bodies = [raw for _, raw, _ in fetched]
    out: list[Paper] = []
    for _, fraw, fresults in fetched:
        page_hash = "sha256:" + hashlib.sha256(fraw).hexdigest()
        for p in _openalex_works(fresults, page_hash):
            out.append(p)
            if len(out) >= want:
                break
        if len(out) >= want:
            break
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    cache_hash = "sha256:" + hashlib.sha256(b"".join(bodies)).hexdigest()
    if capture is not None:
        capture.update({"outcome": "ok", "papers": len(out), "latency_ms": latency_ms,
                        "cache_hash": cache_hash, "pages": len(fetched),
                        "truncated": truncated})
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


def _arxiv_papers(entries: list, page_hash: str,
                  year_from: int | None) -> list[Paper]:
    """Parse one arXiv entry page; year filter applies here, unknown kept."""
    out: list[Paper] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        title = _clean(e.get("title"))
        aid = _clean(e.get("id"))
        if not title or not aid:
            continue
        year = e.get("year")
        year = year if isinstance(year, int) else None
        if year_from and year is not None and year < year_from:
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
    return out


def _arxiv_page(client: httpx.Client, query: str, start: int,
                n: int) -> tuple[bytes, list, int | None]:
    """One arXiv page: raw body, entries, advertised total (None if hidden).

    Foreign shapes (other providers' payloads, direct Atom XML) yield no
    entries rather than a crash: source confusion degrades, never poisons.
    """
    params = {
        "search_query": query,
        "start": str(start),
        "max_results": str(n),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    resp = _get(client, ARXIV_URL, params)
    try:
        body = resp.json()
    except ValueError:
        body = None  # direct Atom XML or any non-JSON body: no papers
    entries = body.get("entries", []) if isinstance(body, dict) else []
    if not isinstance(entries, list):
        entries = []
    total: int | None = None
    if isinstance(body, dict) and isinstance(body.get("total"), int):
        total = body["total"]
    return resp.content, entries, total


def _arxiv_sequential(client: httpx.Client, query: str, want: int,
                      year_from: int | None, out: list[Paper],
                      fetched: list[tuple[int, bytes, list]],
                      round_base: int) -> str | None:
    """Legacy page-at-a-time paging for feeds without a total. Same requests."""
    consumed = sum(len(entries) for _, _, entries in fetched[round_base:])
    pages = len(fetched)
    truncated: str | None = None
    while len(out) < want and pages < _MAX_PAGES:
        asked = min(_PER_PAGE, want - len(out))
        try:
            raw, entries, _ = _arxiv_page(client, query, consumed, asked)
        except (httpx.HTTPError, ValueError) as e:
            truncated = f"{type(e).__name__}: {e}"[:200]
            break
        fetched.append((consumed, raw, entries))
        pages += 1
        if not entries:
            break
        consumed += len(entries)
        page_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        for p in _arxiv_papers(entries, page_hash, year_from):
            out.append(p)
            if len(out) >= want:
                break
        if len(entries) < asked:
            break  # short page: no more results
    return truncated


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
    fetched: list[tuple[int, bytes, list]] = []
    relaxed_used = False
    truncated: str | None = None
    out: list[Paper] = []
    want = min(max(limit, 1), _PER_PAGE * _MAX_PAGES)
    for qi, query in enumerate(queries):
        if qi > 0:
            relaxed_used = True
        round_fetched = len(fetched)
        round_papers = len(out)
        # First page stays sequential: it learns the total and a short page
        # ends small pools without spawning workers. Its failure fails the
        # provider, as before.
        raw, entries, total = _arxiv_page(client, query, 0, min(_PER_PAGE, want))
        fetched.append((0, raw, entries))
        page_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        out.extend(_arxiv_papers(entries, page_hash, spec.year_from))
        if entries and len(entries) >= min(_PER_PAGE, want) and len(out) < want:
            if total is not None and total > len(entries):
                consumed = len(entries)
                short_stop = False
                while (len(out) < want and not short_stop
                       and consumed < total and len(fetched) < _MAX_PAGES):
                    # Top up for year-filtered drops: assume the drop count
                    # repeats, then re-evaluate next wave. No drops observed
                    # means exactly the remaining pages.
                    kept = len(out) - round_papers
                    need = min(_PAGE_WORKERS,
                               (want - kept + (consumed - kept)
                                + _PER_PAGE - 1) // _PER_PAGE,
                               (total - consumed + _PER_PAGE - 1) // _PER_PAGE,
                               _MAX_PAGES - len(fetched))
                    starts = [consumed + i * _PER_PAGE for i in range(need)]
                    with concurrent.futures.ThreadPoolExecutor(
                            max_workers=max(1, len(starts))) as ex:
                        futs = {ex.submit(_arxiv_page, client, query, s, _PER_PAGE): s
                                for s in starts}
                        done: dict[int, tuple[bytes, list]] = {}
                        for fut in concurrent.futures.as_completed(futs):
                            try:
                                fraw, fentries, _ = fut.result()
                            except (httpx.HTTPError, ValueError) as e:
                                truncated = f"{type(e).__name__}: {e}"[:200]
                                for f in futs:
                                    f.cancel()
                                break
                            done[futs[fut]] = (fraw, fentries)
                    advanced = False
                    for s in starts:
                        if s not in done:
                            break  # failure: keep the ordered prefix
                        fraw, fentries = done[s]
                        fetched.append((s, fraw, fentries))
                        consumed += len(fentries)
                        advanced = True
                        page_hash = "sha256:" + hashlib.sha256(fraw).hexdigest()
                        out.extend(_arxiv_papers(fentries, page_hash, spec.year_from))
                        if len(fentries) < _PER_PAGE:
                            short_stop = True  # stale total: pool ends early
                            break
                    if not advanced:
                        break
                    # Year filtering may have dropped kept papers below want
                    # while results remain: the loop tops up while consumed
                    # stays under the advertised total.
            else:
                # No advertised total: legacy sequential paging, same requests.
                t = _arxiv_sequential(client, query, want, spec.year_from,
                                      out, fetched, round_fetched)
                truncated = truncated or t
        if len(out) > round_papers:
            break  # this query produced; never dilute with the relaxed one
    out = out[:want]  # top-up waves may over-fill; the pool stays capped
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    bodies = [raw for _, raw, _ in fetched]
    cache_hash = "sha256:" + hashlib.sha256(b"".join(bodies)).hexdigest()
    if capture is not None:
        capture.update({"outcome": "ok", "papers": len(out), "latency_ms": latency_ms,
                        "cache_hash": cache_hash, "pages": len(fetched),
                        "query_relaxed": relaxed_used, "truncated": truncated})
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
    # Each provider covers the full pool: small runs keep the legacy 25/page,
    # large runs page up to the per-provider ceiling. Providers fetch
    # concurrently; results still assemble openalex-first, deterministically.
    per_provider = min(_PER_PAGE * _MAX_PAGES, max(25, spec.max_papers))

    def run(name_fn) -> tuple[str, list[Paper], dict]:
        name, fn = name_fn
        capture: dict = {"outcome": "error", "papers": 0, "latency_ms": 0.0,
                         "cache_hash": None, "error": None}
        started = time.monotonic()
        try:
            papers = fn(spec, client, per_provider, capture)
            return name, papers, capture  # fn filled outcome/latency/hash
        except (httpx.HTTPError, ValueError) as e:
            capture["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            capture["error"] = f"{type(e).__name__}: {e}"[:300]
            return name, [], capture

    order = (("openalex", openalex_search), ("arxiv", arxiv_search))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(order)) as ex:
        done = {name: (papers, cap)
                for name, papers, cap in ex.map(run, order)}
    providers: dict[str, dict] = {}
    papers: list[Paper] = []
    for name, _ in order:
        got, cap = done[name]
        papers.extend(got)
        providers[name] = cap
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
