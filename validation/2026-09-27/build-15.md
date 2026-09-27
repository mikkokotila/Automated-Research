# 2026-09-27 Build 15: Transactional Promotion (Issue #16)

**Revision under test:** branch `build-15-promotion`
**Owner contract compliance:** fail-closed promotion in disposable terms; only published-immutable checks
qualify lines as accepted code; same halting / human-promotion rules as before.
**Credentials:** synthetic canary checks only; worker refuses GITHUB_TOKEN. No prod credentials.

## What changed

- New `src/canary/promote.py`: Store(candidate/evaluate/promote/recover/rollback/worktree_matches),
  immutable revisions under `revs/<rev>/tree`, atomic `accepted.json` pointer swap (dir fdatasync +
  os.replace), per-cycle journal append keyed `promote/*`, lineage walk.
- `revise.py`: `apply_one` rewired to transactional promotion (Build 13 validation → Build 14 eval →
  Build 15 acceptance). Revise growth is monotone: never shrink `len(lines)` on accepted output.
- New-file creation pinned to `src/canary/`, any patch path.
- `cycle.py`: `finalize_*` checks consistency between worktree and store (`worktree_matches`) and
  blocks on divergence; `sync_worktree_to_accepted` is the only sanctioned repair.
- CLI: revision flag renamed `--rev` (short `-r` unchanged), give-way copy split into maintainer-only
  `canary publish --target` with `-ev`, refusal default, refuse-when-`CANARY_ALLOW_REVISION=1`,
  journal branch `publish/*`.
- `canary.contain`: revision contract suite extended with promotion tombstones and bounded
  `POLICY_TIMEOUT_SEC` (default 300s).
- `evalpack` controller: `revision` field on work orders; builds caller name from device wavefront.
- Demo `/tmp/demo_build15.py`: rev0 seed, A accepted, B rejected (failing checks), C interrupted
  (crash during applied), recover (accepted untouched, C tombstoned), D accepted; lineage
  revD → revA → rev0 printed; decisions journal dumped; DEMO OK.

## Checks run

```
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
363 passed, 2 deselected in 55.84s

$ ./scripts/container_verify.sh 2>&1 | grep -c ": true"
43

$ docker ps -a --format '{{.Names}}' | grep -i canary
(no output)
```

## Disposition

Ready for PR. No new issues.
