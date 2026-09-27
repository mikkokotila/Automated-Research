# Build 06 validation record

Step: #7 — PR: #26
Code: `src/canary/spec.py` (RunSpec/RunBudget/StopReason), `cycle.py`
(budgeted loop + terminal reasons + partials), `muse_client.py` (shared
tracker, strict usage accounting, env bypass removed), `cli.py`
(spec-first validation + budget flags), `report.py` (spec.json, usage,
honest partials), `tests/test_run_budgets.py` (new, 32 tests).

## Evidence

- `pytest -q`: **192 passed, 2 deselected**.
- Demo (`/tmp/demo_build06.py`, scripted): 3-call budget, 1 iteration,
  `q2?` unanswered and never sent (script intact), finalize reserve
  held one call for the synthesis, `run.json` records
  `budget_exhausted` + exact usage + persisted spec.
- Exhaustion without finalize allowance yields synthesis-less partials
  with an explicit placeholder, never a convergence claim.
- CLI: `cycle --max-iterations 0` and `--max-calls 0` exit 2 with
  `invalid input` before any file or network activity.
- Cancellation: pre-start cancel raises; mid-run cancel partals after
  the in-flight step; cancelled dispatch never sends.
- Zero-progress provider refusal still raises (existing contract kept);
  mid-run refusal partals with `provider_blocked`.

## Criteria status

- [x] Invalid specs fail before any call (ranges, model lock, csv/target,
  version, unknown fields); `InvalidSpec` is a `ValueError`.
- [x] Nested work shares one tracker (rebind-or-wrap); broker retries
  each carry reservations; spend-carry shape (`to_dict`/`from_dict`)
  retains calls/tokens with no cap reset. Full resume wiring belongs
  to Build 08 (#9).
- [x] Frozen spec + threaded tracker replace the `MUSE_MAX_CALLS` env
  bypass (removed); cancellation revokes dispatch immediately and stops
  at the next boundary (one in-flight step completes).
- [x] Exhaustion saves partial bundles with remaining questions, exact
  calls/tokens (or explicit `tokens_reported: false`), and the reason.
- [x] Legacy CLI works with finite documented defaults; no unlimited
  unattended path exists (client, spec, and CLI defaults all bounded).

## Notes

- Trust split, stated plainly: per-run budgets ride a frozen spec plus a
  threaded tracker in first-party code (the model sees only text and
  cannot touch them); the exact token ceiling stays broker-side; wall
  time is also launcher-enforced in containment. Revision rounds/patches
  stay refused (Build 02) until a trusted path exists.
- No monetary claims anywhere: unknown pricing is reported as unknown,
  never as zero cost. No invented price basis was introduced.
- `max_iterations` with work remaining now reports `budget_exhausted`
  (one existing assertion migrated to the issue's taxonomy; the
  iteration-cap behavior itself is unchanged).
