# Recovery archive — 26 September 2026

Start with [the continuation handoff](../../docs/HANDOFF.md).

## Contents

| Location | Contents |
|---|---|
| [session/transcript.md](session/transcript.md) | Readable user/Muse conversation with timestamps, original line references, and run endings |
| `session/session.redacted.jsonl` | Full main-session event history, credential-redacted |
| `session/messages.jsonl` / `session/tool-calls.jsonl` | Structured conversation and all 146 committed tool calls |
| `session/tool-outputs/` | All 129 saved tool-output files |
| `session/subagent/` | Five saved child-session JSONL histories |
| `session/cli-*.log` | The session's saved diagnostic log |
| [artifacts/m1-review/](artifacts/m1-review/) | Live cited review and provenance |
| [artifacts/m2-analysis/](artifacts/m2-analysis/) | Live CSV analysis and provenance |
| [artifacts/m3-loop/](artifacts/m3-loop/) | Final synthesis, run manifest, and two per-iteration reviews |
| [inputs/ar-patients.csv](inputs/ar-patients.csv) | The original 300-row synthetic input, not real patient data |
| `working-tree/` | Original source-change status and complete M4 patch relative to M3 |
| `validation/` | Original test evidence and recovery integrity/credential-scan results |
| [manifest.json](manifest.json) | Original source locations, hashes, redaction counts, and archive integrity hashes |

The M4 changes already exist in the repository's normal source/test paths; do not apply the archived patch on top of them. No M4 live reflection or improvement artefacts were found; the saved M1–M3 runs have no procedural journal because they predate that feature.

## Preservation and redaction

This archive copies 145 original files: 136 session/history/diagnostic files, eight report/provenance files, and one synthetic input. All text was scanned before writing. Credential matches were replaced with `[REDACTED_CREDENTIAL]`, including nested JSON/tool arguments. Generated extractions were redacted too. Original reports and the input remain byte-for-byte unchanged where no credentials were present.

Historical absolute paths and internal event IDs remain for provenance. The archived main session retains original line numbering. References to `tool-output://` and original local filesystem paths are not portable; corresponding output files are retained under `session/tool-outputs/`. Redaction can invalidate original embedded byte lengths/digests. This is an evidence archive, not a guaranteed importable live Muse session.

Original local session files were not modified. Runtime locks, SQLite cache/state databases, virtual environments, caches, unrelated sessions, and credentials from outside this session are not included. No agent code or archived shell command was executed as part of recovery.

The manifest hashes identify this fixed snapshot. Current project source may evolve afterward; the `source_snapshot_before_recovery` section documents historical hashes rather than requiring future source files to remain unchanged.
