# Canary

A Python command-line toolkit for cited literature reviews, tabular analysis, and repeatable research cycles.

## Commands

```bash
python -m pip install -e . pytest
canary review "your question" --max-papers 10 --out ./out
canary analyze example.csv --target outcome --out ./out
canary cycle "your question" --max-iterations 3 --out ./out
```

Supply a valid `MUSE_API_KEY` through the environment for live calls. The configured provider and model remain unchanged: the Muse API and `muse-spark-1.3-contributor`. The OpenAI-compatible client library is a transport dependency, not a change of model provider. Never store credentials in this repository or use credentials from historical logs.

`review` writes `review.md` and `provenance.json`. `analyze` writes `analysis.md` and `provenance.json`. `cycle` writes `synthesis.md`, per-step reports, and `run.json`. Workflows also record `journal.jsonl`.

## Maintenance

`canary revise --run-dir ./out --repo /work` assesses a saved journal and proposes bounded revisions. `canary cycle ... --maintenance --revise-rounds 1 --repo /work` enables maintenance between steps. The assessment document is named `assessment.md`.

These modes may modify code and, when a GitHub credential is supplied, invoke the existing publishing integration. Run them only in an appropriately isolated disposable environment. No live publishing or live provider requests were performed during this naming migration. Existing safeguards are unchanged; passing tests is not a containment audit.

The container configuration uses `CANARY_SANDBOXED`. The image tag is `canary:local`; see `scripts/container_run.sh`. Review its resource, network, credential, and output handling before live use.

## Development and evidence

```bash
python -m pytest -q
python -m canary --help
```

Read [the handoff](docs/HANDOFF.md), [contributor guidance](GUIDANCE.md), and [the comparison report](docs/VALIDATION.md). The side-by-side check covers the complete existing test suite and deterministic output contracts with external services mocked. It does not establish identical live text generation after prompt wording changes.

## Historical material

Prior source, tickets, and the recovery archive remain available in Git history. They are intentionally not copied into the current source tree or package. The checkpoint tag is `pre-canary-20260926`. Existing issues retain their numbers and recorded context.
