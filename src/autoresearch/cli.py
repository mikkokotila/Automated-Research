"""CLI: question in, cited review out."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from . import analysis, data as datamod, loop as loopmod, modeling, rank, report, retrieval, synthesize
from .improve import improve_from_journal
from .journal import Journal, emit
from .muse_client import MuseClient
from .spec import ResearchSpec


def review(question: str, max_papers: int, year_from: int | None, out_dir: str) -> str:
    j = Journal(Path(out_dir) / "journal.jsonl")
    emit(j, "review", "start", question[:200])
    spec = ResearchSpec(question=question, max_papers=max_papers, year_from=year_from)
    with httpx.Client(headers={"User-Agent": "Automated-Research/0.1"}) as http:
        papers = retrieval.retrieve(spec, http)
    emit(j, "review", "retrieved", f"{len(papers)} candidates")
    if not papers:
        raise RuntimeError("no papers found for this question")
    top = rank.rerank(spec.question, papers, spec.max_papers)
    synth = synthesize.synthesize(spec.question, top, MuseClient())
    emit(j, "review", "synthesized", f"{len(top)} papers, cited {len(synth.cited)}")
    path = report.write_bundle(out_dir, spec, top, synth, j)
    return str(path / "review.md")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autoresearch", description="Automated researcher, milestone 1.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review", help="run a cited literature review")
    r.add_argument("question", help="research question in plain words")
    r.add_argument("--max-papers", type=int, default=10)
    r.add_argument("--year-from", type=int, default=None)
    r.add_argument("--out", default="./out")
    a = sub.add_parser("analyze", help="test a hypothesis against a CSV file")
    a.add_argument("csv", help="path to CSV data file")
    a.add_argument("--target", required=True, help="target column to predict")
    a.add_argument("--question", default="", help="research question in plain words")
    a.add_argument("--out", default="./out")
    lo = sub.add_parser("loop", help="autonomous run: answers become next questions")
    lo.add_argument("question", help="seed research question in plain words")
    lo.add_argument("--csv", default=None, help="optional dataset for analyze iterations")
    lo.add_argument("--target", default=None, help="target column (with --csv)")
    lo.add_argument("--max-iterations", type=int, default=3)
    lo.add_argument("--max-papers", type=int, default=5)
    lo.add_argument("--out", default="./out")
    lo.add_argument("--self-improve", action="store_true", help="reflect and self-patch mid-run and post-run")
    lo.add_argument("--improve-rounds", type=int, default=1)
    lo.add_argument("--repo", default=".", help="repo checkout to improve (container workspace)")
    im = sub.add_parser("improve", help="reflect on a past run's journal and self-patch")
    im.add_argument("--run-dir", required=True, help="bundle dir containing journal.jsonl")
    im.add_argument("--repo", default=".", help="repo checkout to improve")
    im.add_argument("--rounds", type=int, default=1)
    im.add_argument("--out", default=None, help="optional dir for reflection.md")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "review":
            path = review(args.question, args.max_papers, args.year_from, args.out)
        elif args.cmd == "analyze":
            path = analyze(args.csv, args.target, args.question, args.out)
        elif args.cmd == "improve":
            path = improve(args.run_dir, args.repo, args.rounds, args.out)
        else:
            path = loop(args.question, args.csv, args.target, args.max_iterations, args.max_papers, args.out,
                        args.self_improve, args.improve_rounds, args.repo)
    except Exception as e:  # honest failure, never fake output
        print(f"autoresearch: error: {e}", file=sys.stderr)
        return 1
    print(path)
    return 0


def loop(
    question: str, csv: str | None, target: str | None, max_iterations: int, max_papers: int, out_dir: str,
    self_improve: bool = False, improve_rounds: int = 1, repo: str = ".",
) -> str:
    j = Journal(Path(out_dir) / "journal.jsonl")
    outbox: dict = {}
    res = loopmod.run_loop(
        question, csv, target, max_iterations, max_papers, MuseClient(), journal=j,
        self_improve=self_improve, improve_rounds=improve_rounds, repo_root=repo, outbox=outbox,
    )
    path = report.write_loop_bundle(out_dir, question, res, j)
    if self_improve and outbox.get("improvement"):
        _maybe_publish(repo, outbox["improvement"], j)
        j.save(path / "journal.jsonl")  # persist publish notes too
    return str(path / "synthesis.md")


def _maybe_publish(repo: str, improvement, j: Journal) -> None:
    import os

    if not os.environ.get("GITHUB_TOKEN"):
        emit(j, "publish", "skipped", "no GITHUB_TOKEN; local-only")
        print("publish: skipped (no GITHUB_TOKEN)")
        return
    from .github_ops import GitHub, resolve_repo, resolve_token
    from .improve import new_run_id, publish_round

    doc, rep = improvement
    gh = GitHub(resolve_token(), resolve_repo(repo))
    pub = publish_round(repo, new_run_id(), doc, rep, j, gh)
    print(f"publish: issue={pub.issue_url or 'none'} pr={pub.pr_url or 'none'} merged={pub.merged}")


def analyze(csv: str, target: str, question: str, out_dir: str) -> str:
    j = Journal(Path(out_dir) / "journal.jsonl")
    emit(j, "analyze", "start", f"{csv} target={target}")
    df = datamod.load_csv(csv)
    prep = datamod.prepare(df, target)
    emit(j, "analyze", "prepared", f"{prep.profile.n_rows} rows, {prep.profile.task}")
    res = modeling.run(prep)
    emit(j, "analyze", "modeled", f"{res.best} test={res.best_test} baseline={res.baseline_test}")
    q = question.strip() or f"what predicts {target}?"
    findings = analysis.narrate(q, prep, res, MuseClient())
    emit(j, "analyze", "narrated", f"warnings={len(res.warnings)}")
    path = report.write_analysis_bundle(out_dir, q, csv, target, prep, res, findings, j)
    return str(path / "analysis.md")


def improve(run_dir: str, repo: str, rounds: int, out: str | None) -> str:
    jr = Journal.load(Path(run_dir) / "journal.jsonl")
    outcome = f"past run at {run_dir}: {len(jr)} notes"
    for cand in ("run.json", "provenance.json"):
        p = Path(run_dir) / cand
        if p.exists():
            outcome += f"; {cand}={p.read_text(encoding='utf-8')[:800]}"
            break
    doc, rep = improve_from_journal(jr.text(), outcome, repo, MuseClient(), rounds=rounds, journal=jr)
    dest = Path(out or run_dir)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "reflection.md").write_text(doc.markdown + "\n", encoding="utf-8")
    jr.save(dest / "journal.jsonl")
    print(f"improve: kept={rep.kept} reverted={rep.reverted} skipped={rep.skipped}")
    _maybe_publish(repo, (doc, rep), jr)
    jr.save(dest / "journal.jsonl")
    return str(dest / "reflection.md")


if __name__ == "__main__":
    raise SystemExit(main())
