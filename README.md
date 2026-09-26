# Canary

A Python command-line toolkit for cited literature reviews, tabular analysis, and repeatable research cycles.

## Required request service

Only `muse-spark-1.3-contributor` is permitted. The service enforces a shared ceiling of **200,000,000 tokens in any rolling 24-hour window**, including input, cached input, output and reasoning. The limit is mechanical, not a prompt instruction or spending target.

Start with [the service deployment and accounting contract](docs/TOKEN_BOUNDARY.md). Provider credentials belong only in that service, never in workers. Do not reset its persistent ledger to regain allowance.

## Commands

After deploying the request service:

```bash
./scripts/container_run.sh review "your question" --max-papers 10 --out /work/out
./scripts/container_run.sh cycle "your question" --max-iterations 3 --out /work/out
./scripts/container_run.sh profile --out /work/out/profile
```

The `analyze` command accepts a CSV staged in the disposable workspace and a target column. Review and analysis commands write Markdown and JSON provenance; cycles also write per-step reports and `run.json`. Procedural notes are recorded in `journal.jsonl`.

Optional `--maintenance` and `revise` paths assess notes and test proposed revisions. Their remaining quality and promotion limitations are tracked in the roadmap. The launcher withholds GitHub credentials; publishing requires a separately authorized controller.

## Development and evidence

Run the test suite in the Linux development container with `python -m pytest -q`. `scripts/container_verify.sh` exercises actual network, credential and persistent-ledger boundaries using fixture credentials only. `scripts/profile_cycle.py --fixture` provides an explicitly synthetic timing workload; `--live` requires the deployed service.

Read [the handoff](docs/HANDOFF.md), [contributor guidance](GUIDANCE.md), [the earlier naming comparison](docs/VALIDATION.md), and [current boundary verification](validation/token-boundary/verification.json). An offline fixture does not establish live latency or scientific quality.

## Historical material

The pre-migration source and recovery archive remain in Git history at `pre-canary-20260926`. Current issue numbers and dependency relationships are preserved; current wording uses the Canary interfaces.
