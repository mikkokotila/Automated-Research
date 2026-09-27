# Build 12 validation (Issue #13) — 2026-09-27

## Claim
Every assessment attempt persists a structured record (coverage, evidence,
model, spend) as JSON + Markdown; long journals get chunked review with
explicit unreviewed ranges; patch outcomes file versioned lessons; later
rounds see fresh context; assessment-only runs change no code and need no
trust grant.

## Evidence
- Suite: 306 passed (+14 in `tests/test_assessment.py`), 2 deselected.
- Containment: 43/43 true; zero `canary-*` leftovers.
- Demo `/tmp/demo_build12.py`: 43-note journal → 2 chunks, complete coverage,
  early failure proposed, failed patch reviewed, zero code mutation.

## Behavior changes
- `AssessmentRecord`: id, ts, derived status, seq ranges, journal/code hashes,
  model, calls, merged doc, covered/unreviewed ranges, chunk ids, error.
  Per-chunk + merged records saved for every attempt incl. failures.
- Strict parse: malformed output raises `AssessmentError` (halts rounds with
  a journal note), never a quiet no-op; valid `[]` stays an explicit no-op.
- Budget exhaustion → partial record naming unreviewed ranges; frozen record
  cannot be re-marked complete. Provider refusal/cancel saves then propagates.
- `Memory`: outcome-derived lessons (kept→accepted, else rejected) with
  assessment/code provenance; conflicting results supersede with chains;
  explicit expiry; corrupt files start empty. Lessons render as untrusted
  prompt context only — policy/gate untouched (tested).
- `revise_from_journal`: rounds rebuild input from live notes + prior patch
  outcomes; records + memory saved per round (default `<repo>/runs/...`,
  cycle uses the bundle assessments dir).
- `canary assess --run-dir`: assessment without trust, subprocess, or tree
  mutation (tested with no trust patch). Cycle bundles now persist
  assessment.md instead of discarding the doc.

## Gaps / risks
- Chunk merge caps at 3 proposals (overflow noted + kept in chunk records).
- Text-only inputs (no Journal) carry no seq ranges (marked explicitly).
- Lesson contradiction is target+outcome based; semantic conflicts need a
  human reader. Memory is a snapshot file, not an audit log (journal is).
