# 2026-09-27 Completion-budget escalation on finish=length (Issue #49)

**Revision under test:** branch `fix-49-length-escalation`
**Owner contract compliance:** worker client change; no revision paths
touched; synthetic fixtures only (offline suite); no credentials in
tests; recovery evidence untouched; no archived commands replayed.
Live verification spends owner tokens through the broker ledger only.

## What changed

- `src/canary/muse_client.py`: empty text with `finish=length` now
  escalates the completion budget (double per attempt, worker-owned cap
  32768 mirroring the broker's per-call limit) instead of raising
  immediately. Bounded by the existing `MAX_ATTEMPTS=3`; every attempt
  reserves budget and records receipts as before. Persistent
  exhaustion still raises the unchanged error; empty text with any
  other finish reason stays immediately fatal. Payload is now built
  per attempt so escalation takes effect. No worker→broker import:
  layering preserved.
- `tests/test_provider_boundary.py`: two tests — recovery across
  8000→16000→32000 with per-attempt receipts; persistent exhaustion
  from an explicit 20000 start caps at 32768 and fails honestly.

## Live observation that motivated this

Two identical live `cycle` runs via the trusted launcher stopped
`failed` at iteration 0: one call, 9173 tokens, `finish=length`, empty
text, 61s think — the model spent the whole 8000-token completion
budget on reasoning. A live `review` on another question succeeded,
proving the model path. Broker and ledger behaved correctly
throughout (settled, honest receipts, S2 429 degraded gracefully).

## Checks run

```
$ uv run --no-sync pytest tests/test_provider_boundary.py tests/test_budget.py -q
65 passed, 1 deselected
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
456 passed, 2 deselected in 65.95s
```

Live proof deferred to #39: the same live cycle is re-run after merge
and must converge (or fail differently and honestly).

## Disposition

Ready for PR. Closes #49 on merge; unblocks the #39 live demonstration.
