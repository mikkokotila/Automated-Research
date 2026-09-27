# Build 09 validation (Issue #10) — 2026-09-27

## Claim
Retrieval contracts match current provider docs; every source carries stable
IDs, evidence tiers, and replayable provenance; partial failure and weak
relevance surface as explicit warnings, never as comprehensive coverage.

## Contract findings (verified 2026-09-27, see docs/RETRIEVAL_CONTRACTS.md)
- OpenAlex retired `mailto` (ignored); auth is now a free API key. Fixed.
- S2 has no top-level `doi`; DOI lives in `externalIds.DOI`. Fixed
  (top-level kept as fallback only). Dropped `doi` from requested fields.
- `from_publication_date` filter, `per-page`, S2 `year=YYYY-`, `x-api-key`
  all confirmed current. S2 unauthenticated tier is one shared global pool.

## Evidence
- Suite: 250 passed (+29 in `tests/test_retrieval_contracts.py`), 2 deselected.
- Containment: 43/43 true; zero `canary-*` leftovers.
- Demo `/tmp/demo_build09.py`: overlapping DOIs deduped (3→2), one 429
  retried via Retry-After, off-topic 5000-cite papers flagged
  `weak relevance … citations do not establish relevance`.

## Behavior changes
- `retrieve_with_report`: per-provider outcomes, latencies, response hashes,
  one recorded simplification-only refinement on empty results, dedupe stats,
  coverage verdict, honest warning. `retrieve()` signature unchanged.
- Relevance is a deterministic tripwire (best paper must cover min(3, keywords)
  distinct terms), not an aboutness judge; citations order the baseline but
  are reported separately via `rank_reasons` (+ `citation_driven` flag).
- Evidence tiers `fulltext`/`abstract`/`none`, OA links + licenses recorded
  (linked only, never fetched). Inverted-index gaps mark abstracts truncated.
- Query refinement only drops terms, so drift is impossible by construction.

## Gaps / risks
- No provider API keys provisioned; live runs use anonymous budgets.
- Brokered `/v1/sources/` forwarding unverified live.
- No persistent response cache by design (freshness has no invalidation story).
- Coverage tripwire is keyword-based; sophisticated off-topic sets can pass it.
