"""Report: markdown review/analysis + JSON provenance bundle."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .analysis import Findings
from .data import Prepared
from .modeling import Results
from .papers import Paper
from .spec import ResearchSpec
from .synthesize import Synthesis

if TYPE_CHECKING:
    from .loop import LoopResult


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


def render_analysis(question: str, csv: str, target: str, prep: Prepared, res: Results, f: Findings) -> str:
    p = prep.profile
    lines = [
        f"# Analysis: {question}",
        "",
        f"_Data: {csv} | target: {target} | task: {res.task} | model: {res.best} | narrated by {f.model}_",
        "",
        f.text,
        "",
        "## Evidence",
        "",
        f"- Rows: {p.n_rows}, features: {p.n_features}, missing cells imputed: {p.missing_cells}",
        f"- Metric: {res.metric}; best test {res.best_test} vs baseline {res.baseline_test}",
        "- CV scores:",
    ]
    lines.extend(f"  - {s.name}: {s.cv_mean:.4f} ± {s.cv_std:.4f} (test {s.test:.4f})" for s in res.scores)
    lines += ["- Top features:"] + [f"  - {n}: {v}" for n, v in res.importances]
    if res.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in res.warnings]
    lines.append("")
    return "\n".join(lines)


def write_analysis_bundle(
    out_dir: str | Path,
    question: str,
    csv: str,
    target: str,
    prep: Prepared,
    res: Results,
    f: Findings,
) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "analysis.md").write_text(render_analysis(question, csv, target, prep, res, f), encoding="utf-8")
    p = prep.profile
    provenance = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "analysis",
        "question": question,
        "csv": csv,
        "target": target,
        "model": f.model,
        "task": res.task,
        "metric": res.metric,
        "best": res.best,
        "best_test": res.best_test,
        "baseline_test": res.baseline_test,
        "scores": [{"name": s.name, "cv_mean": s.cv_mean, "cv_std": s.cv_std, "test": s.test} for s in res.scores],
        "importances": [{"feature": n, "value": v} for n, v in res.importances],
        "warnings": list(res.warnings),
        "n_rows": p.n_rows,
        "n_features": p.n_features,
        "missing_cells": p.missing_cells,
        "preprocessing": prep.preprocessing,
        "data_notes": list(p.notes),
    }
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return out


def write_loop_bundle(out_dir: str | Path, seed: str, res: "LoopResult") -> Path:
    out = Path(out_dir)
    iters = out / "iterations"
    iters.mkdir(parents=True, exist_ok=True)
    for i in res.iterations:
        (iters / f"iter{i.n}-{i.kind}.md").write_text(i.detail, encoding="utf-8")
    lines = [
        f"# Autonomous run: {seed}",
        "",
        f"_Iterations: {len(res.iterations)} | stopped: {res.stopped} | model: {res.model}_",
        "",
        res.synthesis,
        "",
        "## Trail",
        "",
    ]
    for i in res.iterations:
        lines.append(f"- iter {i.n} [{i.kind}]: {i.question}")
    if res.unanswered:
        lines += ["", "## Unanswered (carry forward)", ""] + [f"- {q}" for q in res.unanswered]
    lines.append("")
    (out / "synthesis.md").write_text("\n".join(lines), encoding="utf-8")
    provenance = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "loop",
        "seed": seed,
        "stopped": res.stopped,
        "model": res.model,
        "unanswered": list(res.unanswered),
        "iterations": [
            {"n": i.n, "kind": i.kind, "question": i.question, **i.provenance} for i in res.iterations
        ],
    }
    (out / "run.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return out
