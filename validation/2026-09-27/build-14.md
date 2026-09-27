# Build 14 validation (Issue #15) — 2026-09-27

## Claim
A versioned benchmark pack evaluates candidates in isolated workers under a
trusted controller: workers see task inputs only, expectations never leave
the controller, and accept requires zero regressions plus predeclared fixes
resolved. Tampering with outputs, fixtures, or the gate fails closed.

## Evidence
- Suite: 346 passed (+13 in `tests/test_evalgate.py`), 2 deselected.
- Containment: 43/43 true; zero `canary-*` leftovers.
- Demo `/tmp/demo_build14.py`: baseline 13/13, corrective accept,
  regression reject, fake-success reject, decision report saved.

## Behavior changes
- `evalpack/`: 6 task types (relevance, claims, abstention, leakage, resume,
  budget) × dev + acceptance variants = 13 frozen fixtures; thresholds;
  pinned baseline (13/13) with known limitations; operator CLI + README.
- `build_tree`: pristine `git archive` + policy-checked diff (anything
  outside src/canary/ rejects, including the gate); worker ships from the
  controller only — no fixture/expectation file enters the worker tree.
- Workers: scrubbed env (no secrets), PYTHONHASHSEED=0, 180s timeout,
  stdout cap; malformed/crashing/timed-out output fails the fixture.
- `decide`: accept iff no pass-to-pass regressions and every predeclared
  expected-fix (must fail at baseline) resolves; silence never accepted.
- Reports record pack version, thresholds hash, base rev, and per-fixture
  before/after; acceptance reads are access-logged.

## Gaps / risks
- Acceptance inputs live on disk; true secrecy needs an external holder
  (logged reads + versioning are the current control).
- Only deterministic behavior is gated; semantic quality and live variance
  are explicitly out of scope.
- The gate is standalone: revise/promotion does not invoke it yet (later
  build wires promotion to require a gate pass).
