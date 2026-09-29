# Retrieval provider contracts

Verified 2026-09-27 against current primary documentation. No live calls were
made; unit tests and CI use fixtures only.

## OpenAlex

- Docs read: `https://help.openalex.org/api/`,
  `/api/authentication/`, `/api/filtering/`, `/api/deprecations/`.
- Endpoint: `GET https://api.openalex.org/works` with `search` (free text),
  `per-page` (max 100; we cap at 50), `page` (we page to cover large pools,
  8 pages max), and optional
  `filter=from_publication_date:YYYY-MM-DD` (confirmed current syntax).
- Auth: free API key as `api_key` query parameter or `Authorization: Bearer`
  header. **The `mailto` polite pool is retired — the parameter is ignored.**
  We send `OPENALEX_API_KEY` when set, else run on the keyless budget.
- Rate limits: `429` on daily-budget exhaustion or >100 req/s. Responses carry
  usage headers and a `meta` block (not yet consumed).
- Response fields consumed: `results[].id`, `doi` (full URL), `ids.{doi,mag,
  pmid,pmcid}`, `title`, `authorships[].author.display_name`,
  `publication_year`, `cited_by_count`, `primary_location.source.display_name`,
  `abstract_inverted_index`, `open_access.oa_status`,
  `best_oa_location.{pdf_url,landing_page_url,license}`.
- Removed fields we never used: `host_venue`, `grants`, `has_ngrams` filter.

## Semantic Scholar (REMOVED 2026-09-29)

Dropped per owner directive after sustained keyless 429s on 2026-09-28/29
(the unauthenticated global pool saturates on third-party traffic and
self-pacing does not clear it). Worker and broker code removed; retrieval
runs on OpenAlex + arXiv only.

## arXiv

- Docs read: `https://info.arxiv.org/help/api/` (query syntax, sorting,
  paging), `https://info.arxiv.org/help/api/tou/` (terms of use).
- Endpoint: `GET https://export.arxiv.org/api/query` with `search_query`
  (prefix syntax: `all:`, `ti:`, `au:`, `cat:`), `start`, `max_results`,
  `sortBy` (`relevance`, `lastUpdatedDate`, `submittedDate`), `sortOrder`.
  Bare space-separated terms default to OR; we send explicit AND queries
  built from the question's own keywords (see `arxiv_query`).
- Auth: none. Keyless and quota-free; this is the source that carries a run
  when the aggregators rate-limit.
- Rate etiquette: at most one request per 3 seconds. The broker enforces the
  spacing centrally (`serve_arxiv`); guests can never violate it.
- Transport shape: the API returns Atom XML. The broker parses it and serves
  `{"entries": [...]}` JSON with `id` (version stripped; `version` kept
  separately), `title`, `abstract`, `authors`, `year`, `categories`,
  `primary_category`, `doi` (when the record carries one), `url_abs`,
  `url_pdf`. The guest parses that shape only — foreign shapes degrade to
  zero papers, never a crash.
- Wire client: the broker fetches upstream with stdlib urllib, not httpx.
  On 2026-09-28 httpx's compressed keep-alive requests were 406'd while
  urllib/curl passed the same URLs from the same egress within minutes;
  single-term queries passed under both, complex ones only under urllib.
  Do not "unify" this back to httpx without re-verifying live.
- Consumed as `Paper(source="arxiv")`: `citations` is always 0 (arXiv
  reports none; never fabricated), `evidence` is `fulltext` when a PDF link
  is present, and `year_from` is applied client-side (unknown years kept).

## Remaining access blockers

- No `OPENALEX_API_KEY` is provisioned; live use runs on the anonymous
  budget (sustained 429s on 2026-09-28 demoted it to degraded coverage
  while arXiv carries retrieval). Provisioning a free key is the cheapest
  reliability upgrade available.
- The brokered path (`CANARY_GATE_URL` → `/v1/sources/...`) forwards retrieval
  through the request service; broker-side provider keys and forwarding are
  unverified and out of scope for this build.
- Deliberately no persistent response cache: result freshness has no
  invalidation story, so every response is only hashed (`cache_hash`) for
  replay provenance, never stored for reuse.
