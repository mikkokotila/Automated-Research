# 2026-09-27 Failure events in the journal (Issue #44)

**Revision under test:** branch `fix-44-failed-events`
**Owner contract compliance:** CLI + redaction change; no revision paths
touched; synthetic fixtures only; no credentials; recovery evidence
untouched; no archived commands replayed.

## What changed

- `src/canary/cli.py`: new `_journal_failure(out_dir, cmd, error)` —
  appends a terminal `failed` event (`Type: message`, 500 chars) to the
  run's own journal on clean failures. Best-effort (never masks the
  reported error); no-ops without a journal; never appends to sealed
  history; seq continues and run_id is preserved via `Journal.load`;
  redaction + fsync come from `Journal.note`. Wired into the `resume`
  unexpected-exception path and the review/analyze/cycle/revise
  dispatch (owned out dir only — `assess`/`publish` never touch another
  run's history; `ResumeError` refusals stay unjournaled).
- `src/canary/redact.py`: assignment-form patterns now swallow the
  value token (`MUSE_API_KEY = <v>` fully masked, was name-only).
  Strict superset of prior matches; the new disk-write path for
  untrusted error text is covered from day one.
- `tests/test_durability.py`: five tests — review failure journals
  `failed` with continued seq/run_id; resume wiring; sealed history
  untouched; missing-journal no-op; secret-shaped detail fully redacted.

## Checks run

```
$ uv run --no-sync pytest tests/test_durability.py tests/test_export.py -q
30 passed
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
452 passed, 2 deselected in 75.84s
```

## Disposition

Ready for PR. Closes #44 on merge. Crash (no `failed` event) vs clean
failure (`failed` with cause) is now readable from the journal alone.
