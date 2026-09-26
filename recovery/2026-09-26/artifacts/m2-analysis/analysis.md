# Analysis: what predicts mortality in these patients?

_Data: /tmp/ar-patients.csv | target: mortality | task: classification | model: gradboost | narrated by muse-spark-1.3-contributor_

We tested if 4 patient features could predict mortality in 300 patients — 178 coded as 1, 122 coded as 0.

There was no missing data. Numbers were standardized, categories were one-hot encoded. We trained on 75% and tested on 25%.

We compared 3 models on accuracy:
- baseline guessing: cv 0.6000 ± 0.0843, test 0.4667
- logistic regression: cv 0.6800 ± 0.0412, test 0.6933
- gradient boosting, chosen as selected: cv 0.6933 ± 0.0407, test 0.5867
Macro-F1 of 0.555 was also reported.

What predicts the outcome:
Only treatment_delay_wk showed any positive signal at 0.032. The rest showed zero or negative permutation importance, meaning they did not help here: age -0.0027, hpv_neg -0.0027, stage_II -0.008, stage_I -0.008, hpv_pos -0.0107, stage_IV -0.016, stage_III -0.024.

How much to trust it: only a little.
The selected model at test 0.5867 is only slightly above baseline 0.4667.

Warning: it is unstable. Performance dropped a lot from training cv 0.693 to test 0.587, so it may not hold in new patients.

## Evidence

- Rows: 300, features: 4, missing cells imputed: 0
- Metric: accuracy (macro-F1 0.555); best test 0.5867 vs baseline 0.4667
- CV scores:
  - dummy: 0.6000 ± 0.0843 (test 0.4667)
  - logreg: 0.6800 ± 0.0412 (test 0.6933)
  - gradboost: 0.6933 ± 0.0407 (test 0.5867)
- Top features:
  - treatment_delay_wk: 0.032
  - age: -0.0027
  - hpv_neg: -0.0027
  - stage_II: -0.008
  - stage_I: -0.008
  - hpv_pos: -0.0107
  - stage_IV: -0.016
  - stage_III: -0.024

## Warnings

- train/cv vs test gap is large (0.693 vs 0.587) — treat as unstable
