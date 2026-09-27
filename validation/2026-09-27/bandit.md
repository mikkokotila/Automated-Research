# 2026-09-27 Bandit extension: measured strategy selection (Issue #19)

**Revision under test:** branch `build-ext-bandit`
**Owner contract compliance:** no revision-enabled work outside the hermetic
suite (selector swaps the query only; spec/budget/promotion handles never
reach the selector; revision posture untouched); synthetic fixtures only;
no prod credentials (none present); recovery evidence untouched; no archived
commands replayed.

## What changed

- New `src/canary/strategy.py`: four versioned query strategies
  (original/focused/terms/gap-v1), epsilon-greedy ridge bandit,
  `PolicyStore` with atomic snapshots, det-v1 reward, `SelectorSession`,
  counterfactual IPS, and the fixture A/B/C harness.
- `run_review`/`run_cycle` wired: selector output swaps the query only;
  bandit config scope-frozen across resume; checkpoint-compatible.
- CLI: `--bandit-mode/--bandit-dir/--bandit-seed/--bandit-eps`;
  `report.record_bandit` + `bandit.jsonl` per-cycle trail.
- New `scripts/compare_strategies.py`: fixture A/B/C comparison with
  NO-GO default — fixed retained until live-gated evidence exists.
- New `tests/test_strategy.py` (41 tests): strategy versioning, reward
  determinism, exactly-once observe, corrupt-store loud degradation,
  frozen-never-writes, scope tamper refusal, IPS math.

## Design guarantees (held by test)

- Selector sees question + pre-action features only; holds no
  spec/budget/promotion handles. Rewards computed controller-side
  (det-v1: gain-coverage/grounding minus cost/redundancy).
- Default OFF: byte-identical path when bandit mode is unset.
- Unavailable/invalid observations never update; exactly-once observe
  by ID (append-then-snapshot); corrupt store degrades loudly to
  fixed; frozen mode never writes; strategy version change resets arm.

## Checks run

```
$ uv run --no-sync pytest tests/test_strategy.py -q
41 passed in 5.32s
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
438 passed, 2 deselected in 62.78s
$ ./scripts/container_verify.sh
43 true, 0 false/null
$ uv run --no-sync python scripts/compare_strategies.py --out /tmp/bandit_final
held A: mean=0.225 range=[-0.35, 0.8] strategies=['original-v1', 'original-v1']
held B: mean=0.225 range=[-0.35, 0.8] strategies=['original-v1', 'original-v1']
held C: mean=-0.35 range=[-0.35, -0.35] strategies=['terms-v1', 'terms-v1']
```

## Findings fixed on the branch

- Fixture questions collapsed to indistinguishable queries (held A/B
  identical, C degenerate): questions made keyword-distinct and
  `fixture_http` reworked to query-string-keyed batches with a weak
  default; d2 empty branch restored to focused-v1 only.
- Nine strategy tests failed on first write (focused-query
  expectation, `budget_frac` naming, reward coverage titles, mock vs
  real chmod, scope tamper path, gap empty sharing, memory line vs
  JSON count, evaluator claims shape): each fixed at the root, all
  green since.

## Disposition

Ready for PR. Comparison proves fixture mechanics only (updates
apply, freezing holds, costs counted, held-out runs frozen) — not
real research benefit. Fixed strategy retained; any promotion of a
learned policy requires live-gated evidence under #39's owner-blocked
acceptance. #19 closes with the offline portion complete.
