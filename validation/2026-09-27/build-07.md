# Build 07 validation record

Step: #8 — PR: #27
Code: `src/canary/journal.py` (versioned fsync'd notes, sidecars,
redaction, inspect), `src/canary/redact.py` (new choke point),
`src/canary/report.py` (begin/finalize/read_bundle, incremental
records, sealed artefacts), `cycle.py` (`record_dir` wiring),
`cli.py` (manifest-first runs + `inspect`), `tests/test_durability.py`
(new, 13 tests).

## Evidence

- `pytest -q`: **205 passed, 2 deselected** (13 new: versioning,
  impersonation-proof sources, redaction, sidecars + caps, loud
  disk-full, corrupt/legacy loads, concurrent writers, manifest
  lifecycle, bundle-wide secret sweep, legacy/corrupt readers,
  late-manifest finalize, inspect CLI, SIGKILL survival).
- SIGKILL test (real `SIGKILL` to a subprocess mid-run): bundle reopens
  `interrupted` with matching run ids, the completed iteration bundle,
  retrieval evidence, and every acknowledged note.
- Review-kill demo (`/tmp/demo_build07.py`): SIGKILL between retrieval
  and synthesis → `canary inspect` reports `interrupted`, 2 notes,
  kept paper ("Kept evidence"), run ids match.
- Disk-full raises `JournalError` (never silent); corrupt journal lines
  are listed while good notes load; orphan `.tmp` manifests ignored;
  concurrent appends keep all 200 events parseable.

## Criteria status

- [x] Kill/restart retains durable events + completed iterations;
  partial trailing records detected (`truncated`) without loss.
- [x] Disk-full (`JournalError`), malformed input (corrupt-line report),
  concurrent writers (append-safe), interrupted manifests (orphan tmp
  ignored, corrupt reported) all explicit and tested.
- [x] Reports/provenance reference run/event ids and artefact checksums;
  failure paths leave inspectable partial bundles (manifest + notes +
  completed iterations).
- [x] Credential fixtures absent from logs, sidecars, manifests, bundles,
  and error paths (uniform redaction choke point); worker text cannot
  forge the structured trusted source field.
- [x] `read_bundle`/`inspect` support the new schema and report archived
  bundles as `legacy`; nothing is fabricated.

## Notes

- Durability contract: manifest before work, fsync per acknowledged
  note and incremental record, atomic manifest replace, bounded
  sidecars with explicit drop markers, checksummed seal.
- Redaction boundary, stated plainly: credential-shaped strings are
  scrubbed from everything persisted (notes, sidecars, artefacts,
  manifests, specs, errors); the in-memory run is unaffected.
  Best-effort tripwire, not exfiltration-proofing.
- Retention/export: bundles stay local until the maintainer scans
  (`scan_export.py`) and imports them; no retention service added.
