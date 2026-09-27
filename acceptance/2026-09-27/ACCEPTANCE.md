# Acceptance 2026-09-27

Revision: `7719f16a312985e745ca47582c4297f0ada2c772` | Python 3.12.12

| scenario | status | detail |
|---|---|---|
| review-only | pass | cited=1 grounded=1 |
| synthetic-csv | pass | best=logreg test=0.25 seed=810278876 |
| cycle-research | pass | 2 iterations, converged, 5 calls |
| maintenance-refusal | fail | FileExistsError: [Errno 17] File exists: 'acceptance/2026-09-27/scenarios/maintenance-refusal/repo' |
| maintenance-logic | pass | 91 passed in 30.34s |
| crash-resume | pass | crash at call 3, resumed to converged, 5 calls carried |
| budget-exhaustion | pass | stopped honestly at 2/2 calls, q2? carried forward |
| provider-restriction | pass | run blocked honestly; keyless client refuses (CANARY_GATE_TOKEN required; direct provider access is disabled) |
| containment | fail | AssertionError: boundary suite: rc=1 true=0/43 |

Reproduce: `uv sync --locked && uv run --no-sync python scripts/run_acceptance.py`
Live providers and trusted revision are NOT covered here; see DECISION.md.
