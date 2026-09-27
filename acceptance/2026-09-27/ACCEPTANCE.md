# Acceptance 2026-09-27

Revision: `71538d106b475d4cf4261e244382d6f05306019c` | Python 3.12.12

| scenario | status | detail |
|---|---|---|
| review-only | pass | cited=1 grounded=1 |
| synthetic-csv | pass | best=logreg test=0.25 seed=810278876 |
| cycle-research | pass | 2 iterations, converged, 5 calls |
| maintenance-refusal | pass | refused before any work: refusing: revision has no trusted execution path yet (see docs/TRUST_BOUNDARY.md, Builds 03-04); environment flags and container evidence are not authorization |
| maintenance-logic | pass | 91 passed in 30.83s |
| crash-resume | pass | crash at call 3, resumed to converged, 5 calls carried |
| budget-exhaustion | pass | stopped honestly at 2/2 calls, q2? carried forward |
| provider-restriction | pass | run blocked honestly; keyless client refuses (CANARY_GATE_TOKEN required; direct provider access is disabled) |
| containment | pass | 43/43 boundary checks, no leftovers |
| export-scan | pass | 15 files exported, manifest valid, scan clean |

Reproduce: `uv sync --locked && uv run --no-sync python scripts/run_acceptance.py`
Live providers and trusted revision are NOT covered here; see DECISION.md.
