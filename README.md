# Automated Research

Explain a research question in plain words, get a **cited answer** in return.
Milestone 1: automated literature review.

Vision: [Automated Researcher and Beyond](https://medium.com/data-science/automated-researcher-and-beyond-the-evolution-of-artificial-intelligence-5db4fdde6f1c) (2019).
No new science — eloquent stitching of what exists.

## How it works

```
question → OpenAlex + Semantic Scholar → local rerank → Muse synthesis → review.md + provenance.json
```

- **Problem API**: one CLI arg — the question. No solution code from the user.
- **Retrieval**: OpenAlex + Semantic Scholar, deduped by DOI.
- **Rerank**: deterministic local score (term overlap + citations + recency).
- **Synthesis**: Muse API (`https://api.meta.ai/v1`, `muse-spark-1.3`), citations enforced as `[n]`.
- **Governance**: every run emits `provenance.json` (papers, scores, model, timestamp).

## Quickstart

```bash
export MUSE_API_KEY="..."   # also accepts MODEL_API_KEY / META_API_KEY
export SEMANTIC_SCHOLAR_API_KEY="..."  # optional, raises S2 rate limits
pip install -e .
autoresearch review "factors related to mortality in advanced cervical cancer" --max-papers 10 --out ./out
```

Output: `./out/review.md` (the review) and `./out/provenance.json` (the audit trail).

## Development

```bash
pip install -e ".[test]" 2>/dev/null || pip install -e . pytest httpx openai
pytest
```

## Roadmap

- [x] **M1**: cited literature review (this repo state)
- [ ] **M2**: hypothesis test on user CSV (ETL agent + AutoML + validation)
- [ ] **M3**: autonomous loop (answers become next questions)
