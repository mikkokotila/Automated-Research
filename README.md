# Canary

A Python command-line toolkit for cited literature reviews, tabular analysis, and repeatable research cycles.

## Required request service

Only `muse-spark-1.3-contributor` is permitted. The service enforces a shared ceiling of **200,000,000 tokens in any rolling 24-hour window**, including input, cached input, output and reasoning. The limit is mechanical, not a prompt instruction or spending target.

Start with [the service deployment and accounting contract](docs/TOKEN_BOUNDARY.md). Provider credentials belong only in that service, never in workers. Do not reset its persistent ledger to regain allowance.

## Commands

After deploying the request service:

```bash
./scripts/container_run.sh review "your question" --max-papers 10 --out /work/out
./scripts/container_run.sh analyze data.csv --target y --out /work/out
./scripts/container_run.sh cycle "your question" --max-iterations 3 --out /work/out
./scripts/container_run.sh inspect /work/out
./scripts/container_run.sh profile --out /work/out/profile
```

`review` and `analyze` write Markdown plus JSON provenance; `cycle` adds
per-step reports, checkpoints, and `run.json`; every run keeps procedural
notes in `journal.jsonl`. `assess` reviews a past journal without touching
code; with `--assess`, review/analyze/cycle persist that assessment too.
`resume` continues interrupted cycles (see below); `publish` exports a
recorded revision round as a separately authorized maintainer action.

Revision entry points (`--maintenance`, `revise`) refuse on every host:
there is no trusted execution path for live promotion (see [the trust
boundary](docs/TRUST_BOUNDARY.md) and [worker notes](docs/WORKERS.md)).
Revision behavior — validated changesets, evaluation gate, transactional
promotion, interleaved restart — is verified through the hermetic suite,
not live runs. The launcher withholds GitHub credentials; publishing
requires a separately authorized controller.

The launcher enforces wall-time (`CANARY_TIMEOUT_S`, default 1800s), stages
operator-approved wheels read-only at `/wheels` (`CANARY_WHEELS`, installed
with `pip install --no-index --find-links /wheels`) and inputs read-only at
`/inputs` (`CANARY_STAGE`, `https://...` or host-file sources), and writes a
run receipt (`<out>.receipt.json`) with the image digest, limits, and outcome
next to the exported bundle. Guests have no direct egress; host, LAN,
metadata, and public DNS stay unreachable by design.

Every run validates a versioned spec before any external call and enforces
hard budgets (iterations, model calls, tokens, wall time) with distinct
terminal reasons (`converged`, `budget_exhausted`, `insufficient_evidence`,
`no_progress`, `repeated_question`, `cancelled`, `provider_blocked`,
`invalid_input`, `failed`, `restart_required`). Exhaustion saves an honest
partial bundle with remaining questions and exact usage; `cycle
--max-calls`, `--max-tokens`, and `--wall-time-s` tune the finite defaults.

Every run opens with a manifest and an fsync'd event journal, persists
retrieval evidence and each iteration incrementally, redacts
credential-shaped text at every persistence boundary, and seals artefacts
with checksums; `canary inspect <bundle>` reports `completed`, a stopped
reason, `interrupted`, `legacy`, or `corrupt` without executing anything.

Interrupted cycles checkpoint iterations, pending work, seen questions,
budgets, scheduler counters, scope, and code/input hashes after every
iteration and every propose round. `canary resume <bundle>` continues in
place when code and inputs still hash-match, else refuses; `canary resume
<bundle> --fork <new-dir>` continues as an explicitly forked run (history
copied, budgets restarted, parent recorded); `canary resume <bundle>
--expect-revision <rev>` continues a `restart_required` leg research-only
on a verified accepted revision. Finished runs need `--fork`; legacy and
non-cycle bundles cannot resume. Calls cut mid-flight are replayed as
explicit new attempts, never silently dropped.

Bundles leave containment only through the host-side exporter
(`tar | scripts/export_bundle.py | scripts/scan_export.py`), which enforces
bounded regular files, per-file checksums, and a credential scan. Start
with [the operator runbook](docs/RUNBOOK.md).

## Development and evidence

From a fresh checkout, set up the locked environment and run the offline suite with no provider keys present:

```bash
uv sync --locked && uv run --no-sync pytest -q -m "not live"
```

That is the portable offline suite (Python 3.10–3.13). Live provider checks
run via `-m live` (none can pass without a deployed broker).
`scripts/container_verify.sh` exercises actual network, credential, and
persistent-ledger boundaries using fixture credentials only.
`scripts/profile_cycle.py --fixture` provides an explicitly synthetic timing
workload; `--live` requires the deployed service.

End-to-end offline acceptance lives in one command:

```bash
uv run --no-sync python scripts/run_acceptance.py --out acceptance/<date>
```

It runs ten deterministic scenarios (research, crash/resume, budgets,
provider restriction, revision refusal + hermetic revision tests,
containment when Docker exists, export + scan) and seals a dated bundle
with verdicts. No live provider run has been made from this tree; that
remains an explicit release blocker, not a silent gap.

Read [the operator runbook](docs/RUNBOOK.md), [worker notes](docs/WORKERS.md),
[the handoff](docs/HANDOFF.md), [contributor guidance](GUIDANCE.md), [the
earlier naming comparison](docs/VALIDATION.md), and [current boundary
verification](validation/token-boundary/verification.json). An offline
fixture does not establish live latency or scientific quality.

## Historical material

The pre-migration source and recovery archive remain in Git history at `pre-canary-20260926`. Current issue numbers and dependency relationships are preserved; current wording uses the Canary interfaces.
