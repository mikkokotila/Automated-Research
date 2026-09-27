# Build 11 validation (Issue #12) — 2026-09-27

## Claim
Preprocessing fits inside each CV fold on raw fold-train rows; the holdout is
scored once after selection and never influences it; analysis is labeled
predictive association and refuses causal estimation with guidance.

## Evidence
- Suite: 292 passed (+23 in `tests/test_analysis_leakage.py`), 2 deselected.
- Containment: 43/43 true; zero `canary-*` leftovers.
- Demo `/tmp/demo_build11.py`: signal separates from dummy, noise warns
  honestly, grouped splits refused, causal/predictive classified.

## Behavior changes
- `modeling.run` cross-validates per-fold `Pipeline(preprocessor, estimator)`
  on raw train rows; final scoring keeps the train-fit transform (standard
  nested practice, no holdout leak). Regression test records every fit's row
  indices: strict fold-train subsets, holdout never seen.
- Holdout non-influence proven: permuted holdout labels move only reported
  test numbers, never selection or CV means.
- Per-question split seeds rotate the holdout across runs while replaying
  identical (csv, target, question); seed recorded in provenance.
- Causal questions refused with effect-estimation guidance (cycle:
  insufficient_evidence; review CLI: honest error). Importance labeled
  association-everywhere; narration prompt forbids causal relabeling.
- Grouped/time splits explicitly rejected (i.i.d. assumption documented).
- Guards: one-hot explosion cap, sparse densification, finite-score checks
  (winner with failed folds or non-finite holdout raises actionably),
  task override, importances scored with the selection metric.

## Gaps / risks
- Causal detection is keyword-based: explicitly causal phrasing is caught;
  subtly causal intent in association wording is not.
- Same-question reruns reuse the same holdout by design (reproducibility);
  only question drift rotates it.
- Group/time-aware splitting is refused, not supported; repeated-measures
  data must be aggregated by the operator first.
