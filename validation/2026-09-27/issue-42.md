# 2026-09-27 Export CLI hardening (Issue #42)

**Revision under test:** branch `fix-42-export-cli`
**Owner contract compliance:** host-side script change only; no revision
paths touched; synthetic fixtures only; no credentials; recovery evidence
untouched; no archived commands replayed.

## What changed

- `scripts/export_bundle.py`: new `main(argv) -> int` with argparse —
  exactly one `destination` positional, `--help` exits 0, bad argv exits
  2, all with zero side effects. Runtime failures (bad tar, unsafe
  member, limits, collisions, I/O) exit 1 with a one-line
  `export_bundle: error: ...` on stderr. A directory the call created
  is removed on failure; a pre-existing destination (including a lost
  creation race, via `FileExistsError`) is refused and never deleted.
- `tests/test_export.py`: five CLI tests — help, bad argv, non-tar
  stdin (one-line error + no dest left), existing dest, stdin roundtrip.

## Checks run

```
$ uv run --no-sync pytest tests/test_export.py -q
12 passed
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
447 passed, 2 deselected in 67.73s
$ ./scripts/container_verify.sh
exit=0, 43 true
$ python3 scripts/export_bundle.py --help; ls | grep -c help
exit=0, 0 strays
```

## Disposition

Ready for PR. Closes #42 on merge. The `git archive` build-context
suggestion from the issue is deliberately left open for owner triage.
