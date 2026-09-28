# Analysis: what predicts t?

_Data: acceptance/2026-09-28/scenarios/synthetic-csv/input.csv | target: t | task: classification | kind: predictive-association | model: gradboost | narrated by acceptance-scripted_

f1 carries the signal.

## Evidence

- Rows: 80, features: 2, missing cells imputed: 0
- Metric: accuracy (macro-F1 0.335); best test 0.35 vs baseline 0.65
- CV scores:
  - dummy: 0.4833 ± 0.1333 (test 0.6500)
  - logreg: 0.3833 ± 0.0850 (test 0.5000)
  - gradboost: 0.4333 ± 0.1225 (test 0.3500)
- Top predictive features (association, not causation):
  - f2: 0.0
  - f1: -0.16

## Warnings

- best model barely beats the dummy baseline — signal is weak
- lead over baseline is within noise — result may be chance
- small sample (n=80) — wide uncertainty
