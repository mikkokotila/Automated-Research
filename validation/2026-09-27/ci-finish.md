# CI finish validation record

Step: follow-up to #2/#20 — PR: #21
Commit: `84fe54e` (single-step uv provisioning, docs-only skip, unified suite)
CI run: https://github.com/mikkokotila/Canary/actions/runs/36326578787

Result: smoke + matrix 3.10/3.11/3.12/3.13 pass (129 each, incl. containment),
docker pass, no secrets used.

## Measured deltas vs Build 01 baseline

- smoke (3.12): 21s → 20s wall while gaining the containment test (128 → 129).
- matrix per version: ~22s → 16–17s (one setup step and one pytest run fewer).
- docker warm: ~90s → 55s on scoped GHA cache hit.
- docs-only changes: full CI → skipped via paths-ignore (verified no test
  reads docs content: only `test_milestone4.py` uses "README.md" as a
  path-restriction fixture string, never file content).
- CI suite time proper: 8.9s (smoke) / 7.7s (3.10); rest is runner + env setup.

## Local iteration (unchanged commands)

- `uv sync --locked` warm: ~0.4s.
- `uv run --no-sync pytest -q`: 128 passed, 2 deselected, ~12–19s on macOS
  (sklearn-heavy; pytest-xdist trialed and rejected — 16s, slower).
- `pytest -q -m "not live"` collects 129 locally; the containment test passes
  only on Linux (by design, needs `/proc` boot_id + spawn).
