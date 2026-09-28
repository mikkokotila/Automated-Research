# 2026-09-28 Proposal targets validated at record time (Issue #55)

**Revision under test:** branch `fix-55-proposal-targets`
**Owner contract compliance:** assessment + policy refactor; no revision
paths weakened (interlock untouched); synthetic fixtures only; no
credentials; recovery evidence untouched; no archived commands replayed.

## What changed

- `src/canary/changeset.py`: new home of the durable promotion policy
  (`FORBIDDEN_*` + `target_allowed`, moved verbatim from `revise.py`).
  `revise.py` re-exports via import, so all existing users
  (`promote.py`, tests) work unchanged.
- `src/canary/assess.py`: new `validate_proposal_target(target, tree)`
  — shared policy shape + forbidden areas plus existence against the
  tree under discussion (explicit tree, else the running checkout;
  symlinks resolve before containment). Both parsers take `tree=None`:
  strict raises `AssessmentError` naming proposal + reason (callers
  record failed/partial attempts with the error — never silent);
  lenient drops invalid items (established pattern for malformed
  proposals).
- Threading: `assess_journal`/`_assess_one`/`assess` take `tree`;
  `revise_from_journal` passes its repo (covers mid-run and post-run
  maintenance flows — no `cycle.py` change needed);
  `Lifecycle.finish_assessment` takes `tree` (default preserves
  behavior). Plain assess/cycle runs validate against the running tree.
- Tests: 6 new in `tests/test_assessment.py` (strict reject nonexistent
  / accept existing / reject forbidden shape, lenient drop, symlink
  escape, revise end-to-end records `assess-failed` without crashing);
  3 existing tests gained fixture trees with assertions unchanged
  (merge-cap, milestone4 cap, lifecycle twins).

## Live observation that motivated this

Two live runs recorded proposals for `analyze.py`, `providers.py`,
`review.py` — none exist. Every live proposal so far was unactionable.

## Checks run

```
$ uv run --no-sync pytest tests/test_assessment.py tests/test_milestone4.py tests/test_schedule.py tests/test_promotion.py -q
89 passed
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
465 passed, 2 deselected in 66.75s
```

## Disposition

Ready for PR. Closes #55 on merge. Prerequisite in place for #52's demo
ingesting only actionable targets.
