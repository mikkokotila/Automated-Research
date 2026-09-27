# Retrieval provider contracts

Verified 2026-09-27 against current primary documentation. No live calls were
made; unit tests and CI use fixtures only.

## OpenAlex

- Docs read: `https://help.openalex.org/api/`,
  `/api/authentication/`, `/api/filtering/`, `/api/deprecations/`.
- Endpoint: `GET https://api.openalex.org/works` with `search` (free text),
  `per-page` (max 100; we cap at 50), and optional
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

## Semantic Scholar (Academic Graph API)

- Docs read: `https://api.semanticscholar.org/api-docs/graph` (interactive;
  parameter details cross-checked against the official product page and
  consistent client references), `https://www.semanticscholar.org/product/api`.
- Endpoint: `GET https://api.semanticscholar.org/graph/v1/paper/search` with
  `query`, `limit` (max 100; we cap at 50), `fields`, and optional `year`
  (`YYYY-` or `YYYY-YYYY`).
- Auth: optional `x-api-key` header (`SEMANTIC_SCHOLAR_API_KEY`). Unauthenticated
  callers share one global pool (~100 requests / 5 min); saturation is driven
  by third-party traffic and self-pacing does not clear it.
- Response fields consumed: `data[].paperId`, `title`, `abstract`,
  `authors[].name`, `year`, `venue`, `url`, `citationCount`,
  `externalIds.{DOI,ArXiv,MAG,PubMed,...}`, `openAccessPdf.{url,license}`,
  `publicationTypes`. **There is no top-level `doi` field** — DOI is read from
  `externalIds.DOI` (a top-level value is honored only as a fallback).
- Unknown `fields` entries are not requested: `doi` was dropped from the field
  list after this verification.

## Remaining access blockers

- No `OPENALEX_API_KEY` or `SEMANTIC_SCHOLAR_API_KEY` is provisioned; live use
  runs on anonymous budgets (S2's shared pool may saturate).
- The brokered path (`CANARY_GATE_URL` → `/v1/sources/...`) forwards retrieval
  through the request service; broker-side provider keys and forwarding are
  unverified and out of scope for this build.
- Deliberately no persistent response cache: result freshness has no
  invalidation story, so every response is only hashed (`cache_hash`) for
  replay provenance, never stored for reuse.
