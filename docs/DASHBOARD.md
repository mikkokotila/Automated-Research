# Runs dashboard

One table for every run: name it, watch it work, pause it, rerun it, read
its raw log. Served locally by the runs daemon (`runsd`).

## Quickstart

```bash
bash scripts/dashboard_service.sh start
# open http://127.0.0.1:8789/ in a browser
```

The daemon persists across reboots (LaunchAgent). Stop it with
`bash scripts/dashboard_service.sh stop`. Logs: `runs/runsd.out.log`,
`runs/runsd.err.log`.

## Naming runs

Every run gets a name and a brief. Three ways, in order of preference:

1. Dashboard UI: `+ NEW RUN` form (name, brief, cycle fields —
   question, maintenance, profile, max-papers/iterations, revise-rounds,
   max-calls/tokens, wall-time-s — or a raw-args override).
2. `canary runs start --name "..." --brief "..." -- <launcher args>`.
3. Direct launcher use: `RUN_NAME="..." RUN_BRIEF="..." OUT=... \
   bash scripts/container_run.sh ...`.

Without a name the launcher falls back to the output directory key.

## Main table

One row per run: title, run id, started, ended, status, improvements
count, loop-iteration count, research, log, rerun, pause.

- Click the **title** for the full brief plus seed, profile, model, usage,
  patch tally, container, bundle, and launch command.
- Click **Δ** for every code change proposed: target, eval status, reason,
  and PR link once the publish bridge (#66) files it.
- Click **LOOP** for the loop's reformulations in order with statuses.
- **RESEARCH** opens the full research record (`/api/runs/<key>/research`,
  cached per panel, RELOAD refetches): **SYNTHESIS** (headline answer,
  limits, carry-forward questions), **PAPERS** (every retrieved ref with
  title, year, source, abstract, open-access link — capped at 200 with
  counts), **CLAIMS** (per-iteration questions, grounded claims with
  support badges and evidence spans, validation, full review prose), and
  **DECISIONS** (assessor docs plus the complete journal in order).
- **LOG** opens the raw log: the merged journal+ops timeline, the
  timestamped console capture, and the launcher transcript. Live runs
  refresh every few seconds.

## Pause and rerun

**Pause** freezes a live worker container (`docker pause`); the run holds
its budgets and resumes byte-identical on **Unpause**. Only live runs
pause; anything else explains why it cannot.

**Rerun** relaunches the run's recorded launch spec as a new named run
(`<name> (rerun)`). Starts record the exact argv array, so arguments with
spaces round-trip; legacy rows with only a command string re-split it on
a best-effort basis. Adopted history without any spec says so instead of
guessing. Recorded launch env (see below) is re-applied on rerun.

## Launch env (advanced)

Runs sometimes need operator variables such as `CANARY_STAGE`. Three
equivalent ways; all are allowlisted to `CANARY_STAGE`,
`CANARY_TIMEOUT_S`, `CANARY_WHEELS` (anything else is rejected):

1. Dashboard UI: the optional `ENV` field (one `KEY=value` per line).
2. `canary runs start`: picks up allowlisted keys from your shell.
3. API: `POST /api/runs` accepts an `env` object.

The daemon merges them into the launcher child's environment and records
them on the row as `launch_env`. Daemon-managed vars (`OUT`, `RUN_*`,
registry) are never passable. Default launches need none of this.

## Other views

- **Usage**: per-run model calls and tokens with totals.
- **Findings**: currently open issues and PRs (via `gh`, cached a minute).
- **Experiments**: acceptance verdict sets from `acceptance/` (Build 17
  runbook evidence).

## CLI mirror

```bash
canary runs list                    # registry table
canary runs show <key>              # full record
canary runs log <key> [--tail N]    # raw log to stdout
canary runs adopt --bundle <dir>    # register an old bundle
canary runs serve [--port P]        # run the daemon in the foreground
```

## Honest gaps

- Reasoning traces are not exposed by the provider; the log shows every
  recorded event, model-call boundary, and console line instead.
- PR linkage per improvement waits on the publish bridge (#66); entries
  read "not filed" until then.
- The daemon binds loopback only and has no auth: it trusts the owner
  machine. Do not forward its port.
- Registry (`runs/registry.json`) is local and gitignored. Runs outlive
  daemon restarts; listings reconcile phantom "running" rows.
