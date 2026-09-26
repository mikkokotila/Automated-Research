# Canary naming migration: validation

## Result

Both source versions were executed in separate offline Docker containers. The baseline is the full working tree preserved at `pre-canary-20260926`, including the previously uncommitted changes. The candidate is the current Canary source.

| Check | Baseline | Candidate |
|---|---:|---:|
| Existing tests passed | 76 | 76 |
| Failures / errors / skips | 0 / 0 / 0 | 0 / 0 / 0 |
| Comparable output groups | 41 | 41 |
| Unexplained output differences | 0 | 0 |

The same rare-class cross-validation warning occurs in both suites. No assertions were removed or weakened. Test names and paths were migrated with the application names.

The paired check was performed with both the original dependency image and the newly built Canary image. `validation/2026-09-26/comparison.json` records the latter run. The candidate JUnit report is raw. The baseline JUnit file in this tree is explicitly name-normalized; the original raw report and transcript are preserved in the checkpoint's `.migration/` directory and the external migration backup.

## What was compared

The probe compares specification validation, malformed and valid follow-up/assessment parsing, retrieval requests and parsed sources, ranking, complete review/provenance bundles, bounded and converged cycles, classification and regression results, report files, journal events, prompt inputs, CLI help/errors/exit codes, path restrictions, and publishing success/failure/no-change flows.

The publishing checks use a local bare Git repository and mocked GitHub responses. Provider and literature services are mocked. Containers have no network, no host bind mounts, no service credentials, non-root execution, dropped capabilities, a read-only root, temporary workspaces, and resource limits. This is a controlled test arrangement, not a claim that the separate production launcher has been fully audited.

## Permitted differences

Only declared naming substitutions and CLI whitespace normalization are used in the comparison. The probe fixes its clock and uses deterministic input data. It does not erase numerical values, error outcomes, missing files, or request payloads. The full substitution map is versioned in `.migration/rename-map.json` at the checkpoint tag.

The namespace, commands, flags, module symbols, selected output filenames/JSON fields, display strings, and application identity in request metadata intentionally change. Old import paths and CLI spellings are not compatibility aliases. Standard HTTP/GitHub identifiers and real dependency/provider settings retain their required spellings.

This evidence demonstrates matching behavior for the exercised deterministic contracts. It is not a mathematical proof for every possible input. Live generated prose can differ when prompt words change. Live provider calls, real remote publishing, and comprehensive containment validation were not performed.

## Reproduce

From a clean checkout of this revision, with Docker available:

```bash
docker build -t canary:local .
left=$(mktemp -d)
right=$(mktemp -d)
git archive pre-canary-20260926 | tar -x -C "$left"
git archive HEAD | tar -x -C "$right"
python3 scripts/compare_versions.py   --baseline "$left" --candidate "$right"   --mapping "$left/.migration/rename-map.json"   --image canary:local --out /tmp/canary-comparison
```

Use a new output directory on each execution. The driver runs both versions concurrently and exits nonzero on a failed suite or contract difference. Do not reuse historical credentials or replay archived commands.
