# Acceptance 2026-09-28

Revision: `fdabef6c29f7e41895a3c003f70838cd73acb48e` | Python 3.12.12

| scenario | status | detail |
|---|---|---|
| review-only | pass | cited=1 grounded=1 |
| synthetic-csv | pass | best=gradboost test=0.35 seed=2052984197 |
| cycle-research | pass | 2 iterations, converged, 5 calls |
| maintenance-refusal | pass | refused before any work: refusing: revision needs a launcher-provided role (CANARY_GUEST=1 in a disposable worker, or CANARY_PUBLISHER=1 in the owner publish driver); refusing on this h |
| maintenance-logic | pass | 110 passed, 1 skipped in 40.04s |
| crash-resume | pass | crash at call 3, resumed to converged, 5 calls carried |
| budget-exhaustion | pass | stopped honestly at 2/2 calls, q2? carried forward |
| provider-restriction | pass | run blocked honestly; keyless client refuses (CANARY_GATE_TOKEN required; direct provider access is disabled) |
| containment | pass | 47/47 boundary checks, no leftovers |
| export-scan | pass | 15 files exported, manifest valid, scan clean |

Reproduce: `uv sync --locked && uv run --no-sync python scripts/run_acceptance.py`
Live providers and trusted revision are NOT covered here; see DECISION.md.
