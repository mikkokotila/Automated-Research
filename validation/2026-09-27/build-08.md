# Build 08 validation (Issue #9) — 2026-09-27

## Claim
Interrupted cycles resume honestly: checkpoints persist iterations, pending work,
seen questions, budgets, lineage, and code/input hashes; `canary resume`
continues in place on hash match, forks explicitly otherwise, and replays
ambiguous in-flight calls as new attempts.

## Evidence
- Suite: `uv run --no-sync pytest -q -m "not live"` → **221 passed** (205 prior +
  16 new in `tests/test_resume.py`), 2 deselected (live), 1 pre-existing
  macOS-only deselect (`test_processes_share_one_ledger` needs `/proc`).
- Containment: `./scripts/container_verify.sh` → **43/43 true**; zero `canary-*`
  containers left behind.
- Demo `/tmp/demo_build08.py` (synthetic, fault-injected KeyboardInterrupt
  mid-iteration-2): uninterrupted vs interrupted+resumed runs agree on
  questions, summaries, synthesis, and stop reason → **EQUIVALENT**.
- Contract change (intended): duplicate reproposals now stop as
  `insufficient_evidence`, not `converged`; two malformed propose rounds
  (one retry each) stop as `failed` with journaled `propose-unusable`.

## Refusal matrix (all exercised in tests)
- Code changed → in-place refused, `--fork` accepted (budgets restarted).
- Inputs changed → in-place refused, `--fork` accepted.
- Finished run → in-place refused, `--fork` accepted (`forked_from` recorded).
- Legacy / non-cycle / checkpoint-less / short-or-corrupt journal → refused.
- Corrupt checkpoint files skipped; latest schema-valid checkpoint wins.

## Gaps / risks
- Post-checkpoint reservations die with the crash; only the ambiguous-op
  journal note discloses possible remote spend (no double-billing, possible
  under-count of one attempt).
- Resume replays the interrupted step; non-idempotent provider side effects
  could double-execute (documented in the ambiguous-op note).
- Live gate path untested (no `MUSE_API_KEY` provisioned); scripted + mock only.
