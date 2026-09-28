# 2026-09-28 Cycle bundle usage refreshed after post-assessment (Issue #56)

**Revision under test:** branch `fix-56-usage-refresh`
**Owner contract compliance:** CLI accounting fix; no revision paths
touched; synthetic fixtures only; no credentials; recovery evidence
untouched; no archived commands replayed.

## What changed

- `src/canary/cli.py`: `cycle()` refreshes `res.usage` from the shared
  budget after `_post_assess` (`dataclasses.replace` on the frozen
  `CycleResult`), so `run.json` reconciles with the ledger. Review and
  analyze bundles record no usage at all (unchanged); resume writes no
  post-assessment (unchanged). Only the cycle path was stale.
- `tests/test_run_budgets.py`: one end-to-end test driving `cli.cycle`
  with a `MuseClient`-subclass fake (production accounting shape:
  every `complete` reserves + reports) and scripted replies. Asserts
  `run.json` usage equals final budget totals exactly. Proven to fail
  pre-fix (8 calls/60 tokens vs 9/75) and pass post-fix.

## Live observation that motivated this

Live 5-iteration `--assess` cycle: ledger 59,618 vs `run.json` 52,645
(6,973 gap = the 2 post-run assessment calls, correctly ledgered but
snapshotted out of the bundle).

## Checks run

```
$ uv run --no-sync pytest tests/test_run_budgets.py -q
33 passed
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
468 passed, 2 deselected in 67.96s
```

A mid-fix scare (tokens != calls x 15) resolved as test-fake error, not
product bug: `run_cycle` wraps bare completers in `BudgetedCompleter`
but rebinds real `MuseClient`s — exactly one reserve per attempt in
production, confirmed by live attempt counts.

## Disposition

Ready for PR. Closes #56 on merge.
