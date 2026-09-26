"""Report: markdown review + JSON provenance bundle."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .papers import Paper
from .spec import ResearchSpec
from .synthesize import Synthesis


def render_markdown(spec: ResearchSpec, papers: list[Paper], synth: Synthesis) -> str:
    lines = [
        f"# Literature review: {spec.question}",
        "",
        f"_Papers reviewed: {len(papers)} | Model: {synth.model} | "
        f"Cited: {len(synth.cited)}/{len(papers)}_",
        "",
        synth.text,
        "",
        "## Sources",
        "",
    ]
    for i, p in enumerate(papers, 1):
        lines.append(f"{p.cite_line(i)} (score {p.score:.2f})")
    lines.append("")
    return "\n".join(lines)


def write_bundle(out_dir: str | Path, spec: ResearchSpec, papers: list[Paper], synth: Synthesis) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.md").write_text(render_markdown(spec, papers, synth), encoding="utf-8")
    provenance = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question": spec.question,
        "max_papers": spec.max_papers,
        "year_from": spec.year_from,
        "model": synth.model,
        "cited": list(synth.cited),
        "papers": [
            {
                "n": i,
                "ref": p.ref,
                "title": p.title,
                "authors": list(p.authors),
                "year": p.year,
                "venue": p.venue,
                "doi": p.doi,
                "url": p.url,
                "citations": p.citations,
                "source": p.source,
                "score": p.score,
            }
            for i, p in enumerate(papers, 1)
        ],
    }
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return out
