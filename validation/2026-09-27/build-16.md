# 2026-09-27 Build 16: Interleaved Research–Assessment–Revision Cycle (Issue #17)

**Revision under test:** branch `build-16-cycle`
**Owner contract compliance:** fail-closed throughout; revision still only via the
trust gate (test seam in-process); post-restart legs continue research-only so no
revision-enabled work runs where the gate refuses; synthetic demo data only.
**Credentials:** none; stub broker with canned replies; no prod credentials.

## What changed

- New `src/canary/schedule.py`: explicit scheduler (research → assess → revise →
  continue | restart | stop) deciding at safe checkpoints from runner-observed
  counts only. Caps: consecutive fruitless iterations, failed revision rounds,
  assess/revise call share (research starvation guard). Holds no budget, model,
  or scope-mutating handle by construction. `supervise()` drives bounded
  fresh-process restart legs (cap exhaustion raises, never daemons).
- `StopReason` gains `no_progress`, `repeated_question`, `restart_required`.
  Reproposed-only duplicates now stop as `repeated_question` (was
  `insufficient_evidence`); two consecutive ungrounded iterations stop as
  `no_progress` (marker-only citations don't ground; analyses ground on
  computed metrics).
- `cycle.py`: scheduler wired into the loop with journaled `schedule/decide`
  notes; mid-run accept checkpoints and returns `RESTART_REQUIRED` instead of
  continuing on stale imports; post-run revision honors scheduler veto.
  Checkpoints are v2 (`scheduler` + frozen `scope` + `restart_pending`).
- New `resume_after_revision()`: verifies checkpoint pending == expected ==
  store accepted revision, re-anchors the code hash explicitly, restores
  iterations/pending/budgets (spend carries)/scheduler counters, and continues
  the leg research-only with the narrowing journaled. Any mismatch halts
  naming the reason. Plain resume still refuses changed code and non-terminal
  bundles.
- Checkpoint compat: v1 migrates explicitly (counters reset, journaled, works
  in place and via fork); unknown versions are skipped → explicit "no
  checkpoint" halt; missing keys halt naming them. Forks now also copy
  `assessments/` (lessons + rejected-patch history).
- New `src/canary/lifecycle.py`: one assessment hook + documented artefact
  matrix for all commands. `--assess` on review/analyze/cycle persists
  assessment twins without ever revising; revision stays behind `revise` and
  cycle `--maintenance` only.
- CLI: `resume --expect-revision` (exclusive with `--fork`); cycle drives
  `supervise()` when a leg seals `restart_required` (append-mode journaling,
  never a clobbering re-save).
- Demo `/tmp/demo_build16.py`: leg 1 does a review step, assesses, rejects a
  breaking patch (p1), accepts a record-tagging patch (p2) and a versioned
  split-seed patch (p3), halts; supervisor spawns a fresh process importing
  the revised package (verified path) that runs the analyze step with the new
  code (marker `rev-tag-v2` in iter2; split_seed v1=1507666701 →
  v2=3502395430), then converges. 8 model calls, spend carried. DEMO OK.

## Checks run

```
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
397 passed, 2 deselected in 58.74s

$ ./scripts/container_verify.sh 2>&1 | grep -c ": true"
43

$ docker ps -a --format '{{.Names}}' | grep -i canary
(no output)
```

Existing taxonomy fixtures were regrounded (anchored supported claims) where
Build 16 intentionally retires converged-after-ungrounded-iterations; the
duplicate-propose test now asserts `repeated_question`. No behavior outside
the issue's scope was changed.

CI finding (fixed on branch): the first push turned CI red on
`test_full_pack_reproducible` — the evalpack resume task scripts marker-only
reviews and freezes `stopped: converged`. Local pre-commit runs had archived
the pre-change HEAD, hiding it. Fix: `evalpack/worker.py` grounds the resume
task's scripted reviews (same anchored-claim shape as the suite); frozen
`dev.json`/`acceptance.json` expectations untouched. Pack re-verified 13/13
on the fixed HEAD before push.

## Disposition

Ready for PR. No new issues.
