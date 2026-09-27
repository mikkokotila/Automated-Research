# Analysis: what predicts t?

_Data: acceptance/2026-09-27/scenarios/synthetic-csv/input.csv | target: t | task: classification | kind: predictive-association | model: logreg | narrated by acceptance-scripted_

f1 carries the signal.

## Evidence

- Rows: 80, features: 2, missing cells imputed: 0
- Metric: accuracy (macro-F1 0.248); best test 0.25 vs baseline 0.35
- CV scores:
  - dummy: 0.4500 ± 0.1247 (test 0.3500)
  - logreg: 0.5667 ± 0.1225 (test 0.2500)
  - gradboost: 0.5500 ± 0.1000 (test 0.2000)
- Top predictive features (association, not causation):
  - f1: 0.01
  - f2: -0.32

## Warnings

- train/cv vs test gap is large (0.567 vs 0.250) — treat as unstable
- best model barely beats the dummy baseline — signal is weak
- lead over baseline is within noise — result may be chance
- small sample (n=80) — wide uncertainty
