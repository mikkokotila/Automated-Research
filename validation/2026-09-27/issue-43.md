# 2026-09-27 Scanner fail-closed on unscanned content (Issue #43)

**Revision under test:** branch `fix-43-scan-skip`
**Owner contract compliance:** host-side script change only; no revision
paths touched; synthetic fixtures only; no credentials; recovery evidence
untouched; no archived commands replayed.

## What changed

- `scripts/scan_export.py`: `scan_export` now records symlinked paths in
  a new `symlinked` list (targets never followed) and treats any skipped
  oversize file or symlink as unclean — `clean: false`, exit 1. Existing
  report keys keep their shape; the `verify_boundary.py` consumer
  (`clean`/`findings`/`manifest_problems`) is unaffected. Docstring now
  names exactly what is and is not covered.
- `tests/test_export.py`: two tests — a 5MB+ secret-bearing file fails
  closed with `skipped == 1`; a symlink to a secret file fails closed
  with `symlinked == ["notes.txt"]`.

## Checks run

```
$ uv run --no-sync pytest tests/test_export.py -q
9 passed
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
444 passed, 2 deselected in 69.75s
```

## Disposition

Ready for PR (after #46 merges; rebased onto main). Closes #43 on merge.
Maintainers reviewing a now-unclean oversize export must inspect the
flagged files explicitly — that is the intended operator burden.
