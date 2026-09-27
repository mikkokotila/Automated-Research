# Build 10 validation (Issue #11) — 2026-09-27

## Claim
Findings are validated claims anchored to quoted evidence, not bare prose;
unsupported questions end in explicit limitation; follow-ups address recorded
evidence gaps with runner-set lineage; paper text cannot authorize actions.

## Evidence
- Suite: 269 passed (+19 in `tests/test_claims.py`), 2 deselected.
- Containment: 43/43 true; zero `canary-*` leftovers.
- Demo `/tmp/demo_build10.py`: supported/contradicted/unsupported claims
  traced claim → paper → span in the report; unresolved gaps listed for the
  strategist.

## Behavior changes
- `synthesize`: optional ```claims block parsed; spans must anchor verbatim,
  paper indices in range, claim numbers ⊆ quoted numbers, fulltext scope
  requires fulltext evidence; violations downgrade to unsupported with reasons.
  Dangling `[n]` markers reported, never dropped. Semantic support explicitly
  unchecked; overlap_hint labeled fallible.
- No citations + no anchored claims → NoEvidence → insufficient_evidence
  (cycle) / honest error (review). Absence of evidence is not inverted.
- Follow-ups: prompt carries seed objective + harvested gaps; Followup gains
  gap (model) and parent (runner-set); proposals journaled with lineage.
- Summaries append Caveats from non-supported/uncertain claims; reports render
  Validated claims + Validation notes + Unresolved; provenance.json persists
  claims and validation.
- Injection: papers delimited as untrusted data in prompts; last fence wins;
  follow-up parser ignores unknown keys (budgets/tools/credentials); malicious
  questions execute only as search strings (fixture-tested).

## Gaps / risks
- Span anchoring is verbatim containment: paraphrased-but-true claims read as
  unanchored; semantically-wrong-but-quoted claims pass anchoring (disclosed
  as unverified, not proven). No model-based fact check by design.
- Claims block is model-cooperative: absent/malformed blocks fall back to bare
  citations with a rejected/unresolved note.
- Analyze iterations carry no claim records yet (narrated findings only).
