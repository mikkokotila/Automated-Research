"""Report: markdown review/analysis + JSON provenance bundle."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .analysis import Findings
from .data import Prepared
from .journal import Journal
from .modeling import Results
from .papers import Paper
from .redact import redact_text
from .spec import ResearchSpec, StopReason
from .synthesize import Synthesis

if TYPE_CHECKING:
    from .cycle import CycleResult, Iteration
    from .spec import RunSpec

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"


class BundleError(RuntimeError):
    """Run bundle unreadable or unwritable. Never fabricate the missing piece."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(redact_text(json.dumps(obj, indent=2)))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise BundleError(f"manifest not durable: {exc}") from exc


def begin_run(out_dir: str | Path, spec: "RunSpec", kind: str,
              forked_from: str | None = None) -> str:
    """Create the run manifest before any work starts. Returns the run id."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    _atomic_write_json(out / MANIFEST_NAME, {
        "schema_version": MANIFEST_VERSION, "run_id": run_id, "kind": kind,
        "spec": spec.to_dict(), "status": "in_progress",
        "started_at": _utcnow(), "finished_at": None, "artefacts": [],
        "forked_from": forked_from,
    })
    return run_id


CHECKPOINT_VERSION = 1


def code_revision() -> str:
    """Content hash of the worker package; no git required."""
    try:
        root = Path(__file__).resolve().parent
        digest = hashlib.sha256()
        for path in sorted(root.glob("*.py")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return "sha256:" + digest.hexdigest()
    except OSError:
        return "unknown"


def file_hash(path: str | Path | None) -> str:
    """Content hash of an input file, or an explicit missing marker."""
    if path is None:
        return "none"
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as f:
            while chunk := f.read(65536):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return f"missing:{path}"


def write_checkpoint(record_dir: str | Path, state: dict) -> Path:
    """Atomically persist one checkpoint; history is kept, never overwritten."""
    out = Path(record_dir) / "checkpoints"
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"checkpoint-{state['seq']:04d}.json"
    _atomic_write_json(target, {"schema_version": CHECKPOINT_VERSION, **state})
    return target


def read_checkpoints(record_dir: str | Path) -> dict | None:
    """Latest consistent checkpoint, skipping corrupt files. None if absent."""
    out = Path(record_dir) / "checkpoints"
    if not out.is_dir():
        return None
    best: dict | None = None
    for path in sorted(out.glob("checkpoint-*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("schema_version") != CHECKPOINT_VERSION:
            continue
        if not isinstance(state.get("seq"), int):
            continue
        if best is None or state["seq"] > best["seq"]:
            best = state
    return best


def _artefacts(out: Path) -> list[dict]:
    files = []
    for path in sorted(out.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name == MANIFEST_NAME or path.suffix == ".tmp":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(65536):
                digest.update(chunk)
        files.append({"path": str(path.relative_to(out)), "sha256": digest.hexdigest(),
                      "size": path.stat().st_size})
    return files


def finalize_manifest(out_dir: str | Path, status: str) -> dict:
    """Seal the manifest with outcome + artefact checksums. Creates it if absent."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict | None = None
    try:
        manifest = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_VERSION:
        manifest = {"schema_version": MANIFEST_VERSION,
                    "run_id": f"late-{uuid.uuid4().hex[:6]}", "kind": "unknown",
                    "spec": None, "started_at": _utcnow()}
    manifest.update({"status": status, "finished_at": _utcnow(),
                     "artefacts": _artefacts(out)})
    _atomic_write_json(out / MANIFEST_NAME, manifest)
    return manifest


def _append_jsonl(path: Path, obj: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(redact_text(json.dumps(obj)) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        raise BundleError(f"incremental record not durable: {exc}") from exc


def record_retrieval(out_dir: str | Path, question: str, papers: list[Paper],
                     retrieval_report: dict | None = None) -> None:
    """Persist retrieved references immediately; synthesis may never run."""
    _append_jsonl(Path(out_dir) / "retrieval.jsonl", {
        "at": _utcnow(), "question": question,
        "papers": [{"ref": p.ref, "title": p.title, "doi": p.doi, "year": p.year,
                    "source": p.source, "citations": p.citations,
                    "evidence": p.evidence, "oa_url": p.oa_url, "license": p.license,
                    "identifiers": p.identifiers, "abstract": p.abstract,
                    "abstract_complete": p.extra.get("abstract_complete", True),
                    "rank_reasons": p.extra.get("rank_reasons")} for p in papers],
        "report": retrieval_report})


def record_analysis_inputs(out_dir: str | Path, question: str, csv: str, target: str,
                           prep: Prepared, res: Results) -> None:
    """Persist modelling evidence before narration runs."""
    _append_jsonl(Path(out_dir) / "analysis.jsonl", {
        "at": _utcnow(), "question": question, "csv": csv, "target": target,
        "kind": res.kind, "seed": prep.seed,
        "n_rows": prep.profile.n_rows, "task": res.task, "best": res.best,
        "best_test": res.best_test, "baseline_test": res.baseline_test,
        "scores": [{"name": s.name, "cv_mean": s.cv_mean, "cv_std": s.cv_std,
                    "test": s.test} for s in res.scores],
        "warnings": list(res.warnings)})


def write_iteration(out_dir: str | Path, iteration: "Iteration") -> None:
    """Persist one iteration bundle. Idempotent; safe to call twice."""
    iters = Path(out_dir) / "iterations"
    iters.mkdir(parents=True, exist_ok=True)
    (iters / f"iter{iteration.n}-{iteration.kind}.md").write_text(
        redact_text(iteration.detail), encoding="utf-8")
    _atomic_write_json(iters / f"iter{iteration.n}.json",
                       {"n": iteration.n, "kind": iteration.kind,
                        "question": iteration.question, **iteration.provenance})


def read_bundle(out_dir: str | Path) -> dict:
    """Inspect a bundle without executing anything. Legacy stays legacy."""
    out = Path(out_dir)
    report: dict = {"dir": str(out), "status": "legacy", "manifest": None,
                    "manifest_corrupt": False, "journal": {"status": "missing"},
                    "run": None, "spec": None}
    mpath = out / MANIFEST_NAME
    if mpath.exists():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
            if manifest.get("schema_version") != MANIFEST_VERSION or "run_id" not in manifest:
                raise ValueError("unknown manifest schema")
            report["manifest"] = manifest
        except (OSError, ValueError):
            report["manifest_corrupt"] = True
    _, journal_report = Journal.inspect(out / "journal.jsonl")
    report["journal"] = journal_report
    for key, name in (("run", "run.json"), ("spec", "spec.json")):
        try:
            report[key] = json.loads((out / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report[key] = None
    if report["manifest_corrupt"]:
        report["status"] = "corrupt"
    elif report["manifest"] is not None:
        status = report["manifest"].get("status", "in_progress")
        report["status"] = "interrupted" if status == "in_progress" else status
    return report


def render_markdown(spec: ResearchSpec, papers: list[Paper], synth: Synthesis,
                    coverage_warning: str | None = None) -> str:
    lines = [
        f"# Literature review: {spec.question}",
        "",
        f"_Papers reviewed: {len(papers)} | Model: {synth.model} | "
        f"Cited: {len(synth.cited)}/{len(papers)}_",
        "",
    ]
    if coverage_warning:
        lines += [f"> Coverage warning: {coverage_warning}", ""]
    lines += [synth.text, "", "## Sources", ""]
    for i, p in enumerate(papers, 1):
        lines.append(f"{p.cite_line(i)} (score {p.score:.2f})")
    if synth.claims:
        lines += ["", "## Validated claims", ""]
        for claim in synth.claims:
            ev = ", ".join(f"[paper {e.paper}{' ✓' if e.anchored else ' ✗ dangling'}]"
                           for e in claim.evidence) or "no evidence declared"
            lines.append(f"- {claim.id} ({claim.support}, scope {claim.scope or '?'}): "
                         f"{claim.text}")
            lines.append(f"  - evidence: {ev}")
            if claim.uncertainty:
                lines.append(f"  - uncertainty: {claim.uncertainty}")
    validation = synth.validation or {}
    if validation.get("rejected") or validation.get("dangling_citations"):
        lines += ["", "## Validation notes", ""]
        for marker in validation.get("dangling_citations", []):
            lines.append(f"- dangling citation [{marker}]: no such paper provided")
        for problem in validation.get("rejected", []):
            lines.append(f"- rejected: {problem}")
    if validation.get("unresolved"):
        lines += ["", "_Unresolved: " + "; ".join(validation["unresolved"]) + "_", ""]
    lines.append("")
    return "\n".join(lines)


def write_bundle(
    out_dir: str | Path, spec: ResearchSpec, papers: list[Paper], synth: Synthesis, journal: Journal | None = None,
    coverage_warning: str | None = None,
) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if journal is not None:
        journal.save(out / "journal.jsonl")
    (out / "review.md").write_text(redact_text(render_markdown(spec, papers, synth, coverage_warning)),
                                   encoding="utf-8")
    provenance = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question": spec.question,
        "max_papers": spec.max_papers,
        "year_from": spec.year_from,
        "model": synth.model,
        "cited": list(synth.cited),
        "coverage_warning": coverage_warning,
        "claims": [asdict(c) for c in synth.claims],
        "validation": synth.validation,
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
                "evidence": p.evidence,
                "oa_url": p.oa_url,
                "license": p.license,
                "identifiers": p.identifiers,
                "rank_reasons": p.extra.get("rank_reasons"),
            }
            for i, p in enumerate(papers, 1)
        ],
    }
    (out / "provenance.json").write_text(redact_text(json.dumps(provenance, indent=2)),
                                         encoding="utf-8")
    if journal is not None:
        journal.trusted_note("bundle", "sealed", "artefacts finalized")
        journal.save(out / "journal.jsonl")
    finalize_manifest(out, "completed")
    return out


def render_analysis(question: str, csv: str, target: str, prep: Prepared, res: Results, f: Findings) -> str:
    p = prep.profile
    lines = [
        f"# Analysis: {question}",
        "",
        f"_Data: {csv} | target: {target} | task: {res.task} | kind: {res.kind} | "
        f"model: {res.best} | narrated by {f.model}_",
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
    lines += ["- Top predictive features (association, not causation):"] + [f"  - {n}: {v}" for n, v in res.importances]
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
    journal: Journal | None = None,
) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if journal is not None:
        journal.save(out / "journal.jsonl")
    (out / "analysis.md").write_text(redact_text(render_analysis(question, csv, target, prep, res, f)),
                                         encoding="utf-8")
    p = prep.profile
    provenance = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "analysis",
        "question": question,
        "csv": csv,
        "target": target,
        "model": f.model,
        "task": res.task,
        "analysis_kind": res.kind,
        "split_seed": prep.seed,
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
    (out / "provenance.json").write_text(redact_text(json.dumps(provenance, indent=2)),
                                         encoding="utf-8")
    if journal is not None:
        journal.trusted_note("bundle", "sealed", "artefacts finalized")
        journal.save(out / "journal.jsonl")
    finalize_manifest(out, "completed")
    return out


def write_cycle_bundle(out_dir: str | Path, seed: str, res: "CycleResult",
                       journal: Journal | None = None, spec: "RunSpec | None" = None) -> Path:
    out = Path(out_dir)
    if journal is not None:
        journal.save(out / "journal.jsonl")
    for i in res.iterations:
        write_iteration(out, i)
    stopped = res.stopped.value if isinstance(res.stopped, StopReason) else res.stopped
    usage = res.usage or {}
    usage_line = (f"model calls: {usage.get('model_calls', '?')}, "
                  f"tokens: {usage.get('tokens', '?')}"
                  f"{'' if usage.get('tokens_reported') else ' (tokens not reported by client)'}")
    lines = [
        f"# Unattended run: {seed}",
        "",
        f"_Iterations: {len(res.iterations)} | stopped: {stopped} | model: {res.model}_",
        f"_{usage_line}_",
        "",
        res.synthesis or "_No final synthesis: see stopped reason and unanswered questions._",
        "",
        "## Trail",
        "",
    ]
    for i in res.iterations:
        lines.append(f"- iter {i.n} [{i.kind}]: {i.question}")
    if res.unanswered:
        lines += ["", "## Unanswered (carry forward)", ""] + [f"- {q}" for q in res.unanswered]
    lines.append("")
    (out / "synthesis.md").write_text(redact_text("\n".join(lines)), encoding="utf-8")
    provenance = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "cycle",
        "seed": seed,
        "stopped": stopped,
        "model": res.model,
        "unanswered": list(res.unanswered),
        "usage": usage,
        "spec": spec.to_dict() if spec is not None else None,
        "iterations": [
            {"n": i.n, "kind": i.kind, "question": i.question, **i.provenance} for i in res.iterations
        ],
    }
    (out / "run.json").write_text(redact_text(json.dumps(provenance, indent=2)), encoding="utf-8")
    if spec is not None:
        (out / "spec.json").write_text(redact_text(json.dumps(spec.to_dict(), indent=2)),
                                       encoding="utf-8")
    if journal is not None:
        journal.trusted_note("bundle", "sealed", f"stopped={stopped}")
        journal.save(out / "journal.jsonl")
    finalize_manifest(out, stopped)
    return out
