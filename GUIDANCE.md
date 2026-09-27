# Contributor guidance

Read `docs/HANDOFF.md`, `docs/VALIDATION.md`, `docs/TRUST_BOUNDARY.md`, `docs/RUNBOOK.md`, and `docs/WORKERS.md` before making changes.

- Keep the CLI and module names consistent with the current `canary` package.
- Preserve the configured provider unless the owner authorizes a change. Never reuse credentials from archived material.
- Do not execute generated revisions on the host. Public revision entry points refuse on every host; no environment flag or container evidence authorizes them. Exercise revision logic through the hermetic suite only.
- Keep changes scoped; a naming migration is not a reason to change permissions, limits, validation, or publishing policy.
- Keep historical material intact in its checkpoint tag; do not replay historical commands.
- Record the exact tested revision and distinguish mocked checks from live demonstrations.
- Use only public or synthetic data for initial checks. Sample reports are not clinical conclusions.
- Run `scripts/run_acceptance.py` before release-sized changes and keep its scenarios deterministic and credential-free.

- Read `docs/TOKEN_BOUNDARY.md`. The fixed 200,000,000-token rolling window and exact model lock belong to the external service. Never reset its volume or add direct provider access to a worker.
