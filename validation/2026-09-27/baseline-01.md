# Build 01 baseline validation record

Step: #2 — PR: #20
Code commit: `42b27980b35435fd12ab992f365b0e35e504d18e`
CI run (push): https://github.com/mikkokotila/Canary/actions/runs/36298324436
Result: test matrix 3.10/3.11/3.12/3.13 pass, docker job pass, no secrets used.

## Local disposable runs (no provider keys in environment)

- Fresh locked env: `uv sync --locked` (uv 0.9.13), uv.lock sha256
  `ee292565292702eaae532ab8b3efa4836f38441cbfdacc430506d8d2ad6e2ec3`.
- Interpreter/platform: CPython 3.12.12, macOS-26.6.2-arm64.
- Pinned versions exercised: pytest 8.4.2, pandas 3.0.6, scikit-learn 1.9.1,
  numpy 2.5.3, httpx 0.28.1, openai 3.19.2.
- `uv run pytest -q`: **128 passed, 2 deselected**, exit 0.
- Container (`canary:build01`, image sha256:6003e1…, `--network none`,
  read-only): **128 passed, 2 deselected**, exit 0.
- `pytest -q -m containment` in Linux container: **1 passed** (multiprocess
  ledger sharing over real Linux boot_id clock), exit 0.

## Original-53 accounting

Source: `recovery/2026-09-26/validation/original-session-last-tests.txt`
at `pre-canary-20260926` (53 passed, historical, preserved untouched).

- 37 tests unchanged by name and still passing in `tests/test_milestone{1..4}.py`.
- 16 tests renamed 1:1 by the Canary migration (`loop*`→`cycle*`,
  `reflect*`→`assess*`, `improve_from_journal*`→`revise_from_journal*`,
  `journal_text_tail_caps`→`journal_text_keeps_head_and_tail`); behavioral
  equivalence established in `docs/VALIDATION.md` (76=76 paired run).
- 11 tests added between recovery and migration (muse/gate client, retrieval
  retry, CSV edge cases, durable journal); none deleted.
- `tests/test_autonomy.py` (12) at the checkpoint is byte-identical in test
  names to current `tests/test_operation.py` (rename only).
- `tests/test_budget.py` (25) and `tests/test_offline_fixtures.py` (4 + 1
  live-template skip) are new since the checkpoint.
- Current total: 128 offline + 1 containment + 1 live template = 130 collected.

## Known limitations (not fixed here)

- The rare-class cross-validation `FitFailedWarning` fixture warning persists;
  pre-existing, documented in `docs/HANDOFF.md`.
- Green tests do not establish host isolation, live provider behavior, or
  scientific validity. Live acceptance stays gated on steps 05–06.
