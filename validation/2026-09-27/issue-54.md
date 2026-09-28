# 2026-09-28 Citation markers exempt from claim number check (Issue #54)

**Revision under test:** branch `fix-54-cite-numbers`
**Owner contract compliance:** validator change; no revision paths
touched; synthetic fixtures only; no credentials; recovery evidence
untouched; no archived commands replayed.

## What changed

- `src/canary/synthesize.py`: `validate_claims` strips `[(n)]`
  citation markers from claim text before number extraction. A
  verbatim-anchored, correctly-cited claim no longer dies on its own
  marker digit; genuinely invented numbers still fail, and dangling
  markers are still reported via `dangling_citations`.
- `tests/test_claims.py`: two tests — the live iter1 shape (anchored
  claim with trailing `[1]`, digit-free span) stays supported; `42%`
  beside a marker is still rejected.

## Live observation that motivated this

Live 5-iteration run: iter1 scored 0/12 grounded on relevant papers
(top hit AutoCodeRover, on-question) because every claim text ended in
a citation marker. Iter2's marker-free claims on the same papers passed.

## Checks run

```
$ uv run --no-sync pytest tests/test_claims.py -q
21 passed
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
461 passed, 2 deselected in 69.15s
```

## Disposition

Ready for PR. Closes #54 on merge.
