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

## arXiv full-text (lazy)

- Route: `GET /v1/sources/arxiv-pdf?id=<arxiv-id>` (brokered only; no
  direct-fetch fallback). Strict id validation; anything else is a 403
  without an upstream call. Upstream failures map to 502
  `source_unavailable`, same as the search route.
- The broker fetches `https://arxiv.org/pdf/<id>` under the shared 3s
  courtesy lock, checks `%PDF` magic (20MB cap), extracts text with pypdf
  (100k-char cap with `truncated` flag), and serves JSON: `id`, `text`,
  `pages`, `truncated`, `bytes`, `sha256`, `source_url`, `rights` (always
  `""`: per-paper license is NOT determined — verify at `source_url`
  before redistributing), `cache_hit`, `fetched_at`.
- Cache: `$CANARY_FULLTEXT_CACHE` (default `/state/fulltext` on the gate's
  persistent volume), `<id>.pdf` + `<id>.json` pairs, atomic writes,
  oldest-first eviction over 2GB. This is the deliberate exception to the
  no-persistent-cache rule below: arXiv PDFs are immutable per id+version,
  so no invalidation story is needed.
- Guest: `fulltext.enrich` attaches text to the first `--max-fulltext`
  arXiv papers per review (default 2, 0 disables); synthesis prompts carry
  an 8k-char excerpt and claim spans anchor against the full text. Every
  failure degrades to abstract-only with counts in a `review/fulltext`
  journal event. Without a broker the step is a no-op.

## Remaining access blockers

- `OPENALEX_API_KEY` lives broker-side only (gate env, never guest-visible):
  the broker injects it into upstream calls when set, else runs on the
  anonymous budget (sustained 429s on 2026-09-28 demoted it to degraded
  coverage while arXiv carries retrieval). The worker sends `api_key`
  itself only on direct (non-brokered) calls.
- The brokered path (`CANARY_GATE_URL` → `/v1/sources/...`) forwards retrieval
  through the request service; broker-side provider keys and forwarding are
  unverified and out of scope for this build.
- Deliberately no persistent response cache: result freshness has no
  invalidation story, so every response is only hashed (`cache_hash`) for
  replay provenance, never stored for reuse.
