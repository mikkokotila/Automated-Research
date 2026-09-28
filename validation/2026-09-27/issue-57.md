# 2026-09-28 Learning policy must live under the record dir (Issue #57)

**Revision under test:** branch `fix-57-bandit-persist`
**Owner contract compliance:** selector guard + help text; no revision
paths touched; synthetic fixtures only; no credentials; recovery
evidence untouched; no archived commands replayed.

## What changed

- `src/canary/strategy.py`: `SelectorSession.__init__` refuses fast
  (`BanditError`, outside the degrade-and-continue path) when learning
  mode names a policy dir outside the record dir — such updates could
  never export. Frozen may read anywhere (never writes); off/fixed
  never touch the store; fixture harnesses without a record dir are
  unconstrained. The only production construction site
  (`run_cycle`, always with a record dir) propagates the refusal, so a
  misconfigured run fails at startup with the remedy.
- `src/canary/cli.py`: `--bandit-dir` help now states the rule and the
  cross-run handoff: learn into `$OUT/bandit` (exports with the
  bundle), stage `snapshot.json` + `observations.jsonl` via
  `CANARY_STAGE`, run frozen against `/inputs`.
- `tests/test_strategy.py`: 3 new tests (outside fails fast, inside
  allowed + snapshot written, frozen-outside allowed); 2 existing tests
  moved their policies inside the record dir with assertions unchanged
  (provenance content, snapshot resumability).

## Live observation that motivated this

Live learning cycle with `--bandit-dir /tmp/ops-live/bpol-trust` (a
host path, meaningless in-container) completed healthy-looking with 5
decisions + observations in `bandit.jsonl`, but no policy exists
afterwards — learning evaporated with the container, silently.

## Checks run

```
$ uv run --no-sync pytest tests/test_strategy.py -q
44 passed
$ uv run --no-sync python scripts/compare_strategies.py --out /tmp/bandit_recheck
held/means/costs identical to the pre-fix baseline
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
470 passed, 2 deselected in 66.81s
```

## Disposition

Ready for PR. Closes #57 on merge. Warm-start learning from a staged
prior (writable seeding) remains future work; frozen-from-staged is the
sanctioned cross-run path as of this change.
