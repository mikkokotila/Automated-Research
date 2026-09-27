# Build 13 validation (Issue #14) — 2026-09-27

## Claim
Every candidate diff passes structural parsing plus policy plus git's own
inspection in a disposable checkout before any apply; application binds to an
exact base and file hashes; the tree must match the manifest afterward; the
promotion policy keeps tests, launcher, broker, credentials, evidence, and
the gate itself outside worker control.

## Evidence
- Suite: 333 passed (+27 in `tests/test_changeset.py`), 2 deselected.
- Containment: 43/43 true; zero `canary-*` leftovers.
- Demo `/tmp/demo_build13.py`: one allowed patch with manifest hashes and a
  7-row rejection table; no candidate code executed.

## Behavior changes
- New `src/canary/changeset.py` (itself a protected file): structural parse
  rejects binary/mode/rename/copy/delete/quoted/alias/malformed-hunk/empty/
  oversize/duplicate-path diffs; headerless single-file diffs accepted.
- `verify_in_disposable`: pristine worktree at HEAD, prior kept diffs
  replayed, `git apply --check`, then manifest with per-file old/new hashes.
- `apply_one`: parse → policy → disposable → HEAD/hash binding → apply →
  manifest-state check → checks → extras check (pycache-tolerant) → kept.
  Any drift rejects/reverts without editing.
- Policy extended: boundary/, scripts/, recovery/, validation/, .env*,
  *.pem/*.key, pyproject.toml, trust/token docs, changeset.py now forbidden;
  only src/canary/ worker code patchable.
- `request_diff` sends base rev + file hash + redacted content and records
  omissions; candidate provenance (proposal, manifest, redacted diff,
  context) persisted per attempt without secrets.
- Sequential same-round patches validate against HEAD+prior-kept replay.

## Gaps / risks
- TOCTOU between binding check and apply is small but nonzero (same-process
  attacker out of scope; containment is the real boundary per TRUST_BOUNDARY).
- New-file creation under src/canary/ is allowed; directory creation outside
  it is blocked by policy, not by filesystem sandboxing.
- Checks run with the worker's privileges inside the trusted path; a
  malicious test file cannot exist (tests/ immutable) but check_cmd itself
  is operator-supplied.
