# Evaluation gate (benchmark pack v1.0.0)

Independent check on worker-proposed code: the candidate runs in disposable
worker trees, and a trusted controller scores outputs against frozen
expectations the candidate never sees.

## What is deterministic, fallible, or live

- **Deterministic (this pack):** relevance verdicts, claim anchoring,
  abstention behavior, per-fold fit indices, resume equivalence, budget
  exhaustion. Fixed seeds, fixed inputs, exact-match scoring.
- **Fallible (kept out of the verdict):** keyword-overlap hints, model
  narrations, anything a language model asserts. The gate never asks the
  candidate to grade itself.
- **Live (explicitly uncovered):** provider behavior, network variance,
  containment strength. A green gate says nothing about those.

**Green tests ≠ proof.** A pass is non-regression evidence against 13 frozen
fixtures, not a certificate of correctness, scientific truth, or containment.

## Layout

- `worker.py` — runs one task in the worker tree (controller-owned copy).
- `controller.py` — builds trees, runs workers, scores, decides.
- `run.py` — operator CLI (`--baseline`, `--diff`, `--expect-fix`).
- `dev.json` — iteration fixtures (inputs + frozen outputs).
- `acceptance.json` — acceptance fixtures; reads are access-logged.
- `thresholds.json` — frozen decision inputs (version, timeouts, accept rule).
- `baseline.json` — pinned baseline results + known limitations.

## Rules

1. Expectations are frozen before any candidate runs; changing them needs a
   pack version bump and a fresh baseline.
2. `expected_fixes` are declared before the candidate runs and must fail at
   baseline. Unclaimed newly-passing fixtures never count as improvement.
3. The worker tree gets `worker.py` only — never `dev.json`, `acceptance.json`,
   or `baseline.json`. Expected outputs cannot be read to forge matches.
4. Worker env is scrubbed (no tokens), hashed deterministically
   (`PYTHONHASHSEED=0`), and bounded (timeout, stdout cap).
5. Candidate diffs are policy-checked before they touch a tree; anything
   outside `src/canary/` (including this gate) rejects the candidate.
6. Worker stdout is data: malformed, crashing, or timed-out workers fail
   their fixtures. Files a worker writes are never read as results.

## Known limits

Acceptance inputs live on disk next to the gate; true secrecy needs an
external holder. Until then, every acceptance read is logged (timestamp,
reason, pack version) and repeated unlogged iteration against acceptance is a
process violation, not a technical impossibility.
