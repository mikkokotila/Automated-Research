# Canary continuation handoff

## Scope of this checkpoint

The project was renamed without changing its research, analysis, revision, or publishing algorithms. The namespace is `canary`, the repeat controller is `cycle`, the journal evaluator is `assess`, and the patch controller is `revise`. The opt-in flag is `--maintenance`. The current package deliberately contains no compatibility aliases for the retired application names.

The pre-migration snapshot includes all previously uncommitted source, tests, scripts, and container configuration, not just the older recovery commit. It is preserved under `pre-canary-20260926`. Original recovery documents remain in that tag; they have not been rewritten or represented as current results.

## Current behavior

The package retrieves literature from OpenAlex and Semantic Scholar, ranks sources, asks the configured Muse service for a cited review, optionally fits and evaluates tabular models, and proposes follow-up questions. It records procedural notes and can assess those notes, propose code patches, test them, and keep or revert them.

The newer pre-migration work also includes a container launcher and a GitHub publishing integration. Maintenance can create issues and pull requests when configured with a GitHub credential. This migration preserves that behavior but does not authorize live execution or certify its design. The offline contract checks use a local bare Git repository and mocked GitHub responses; no real publishing workflow was executed.

## Current names and interfaces

| Purpose | Name |
|---|---|
| Package and executable | `canary` |
| Repeated research command/module | `cycle` |
| Optional maintenance flag | `--maintenance` |
| Revision command/module | `revise` |
| Revision count | `--revise-rounds` |
| Assessment module | `assess` |
| Assessment document | `assessment.md` |
| Container environment marker | `CANARY_SANDBOXED` |
| Container image | `canary:local` |

Existing client scripts, imports, saved plans, and output consumers must adopt these names. The historical mapping is stored with the checkpoint rather than embedded as aliases in current code. Historical CLI commands and assessment JSON use the previous schema; they are not automatically accepted by the new parser. Preserve archived runs; migrate a copy when required.

## Invariants retained

The model identifier, provider URL, credential variable names for external services, GitHub REST/GraphQL fields, HTTP header names, numerical algorithms, iteration limits, path restrictions, and exit decisions are unchanged except for the declared application naming migration. Standard protocol identifiers and actual dependency/provider names are not disguised.

The requested provider remains Muse with `muse-spark-1.3-contributor`. The historical provider restriction is not bypassed by a rename. Confirm legitimate access before any live call. No credential was revoked, replaced, or exercised in this migration.

## Remaining work

Continue the existing issues from the repository roadmap. Issue numbers and dependency relationships survive the repository rename. Some issue descriptions deliberately describe historical paths; consult the checkpoint or the mapping when implementing them.

A green regression suite is not proof of sound scientific inference, safe network boundaries, or resistance to evaluator tampering. The environment marker and container-presence check are unchanged, and do not establish isolation by themselves. The recorded warning on a rare-class cross-validation fixture appears in both versions. It is an existing limitation, not a newly introduced difference.

Live provider behavior, real GitHub publishing, end-to-end containment, and old saved-plan migration were not validated by this naming-only comparison. Differences in prompt words can change live generated prose even when deterministic control flow is equivalent. Read `docs/VALIDATION.md` for the exact evidence and normalization rules.
