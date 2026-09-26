# Contributor guidance

Read `docs/HANDOFF.md` and `docs/VALIDATION.md` before making changes.

- Keep the CLI and module names consistent with the current `canary` package.
- Preserve the configured provider unless the owner authorizes a change. Never reuse credentials from archived material.
- Do not execute generated revisions on the host. The environment marker is not proof of containment.
- Keep changes scoped; a naming migration is not a reason to change permissions, limits, validation, or publishing policy.
- Keep historical material intact in its checkpoint tag; do not replay historical commands.
- Record the exact tested revision and distinguish mocked checks from live demonstrations.
- Use only public or synthetic data for initial checks. Sample reports are not clinical conclusions.
