"""CLI: question in, cited review out."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from . import analysis, data as datamod, cycle as cyclemod, modeling, rank, report, retrieval, synthesize
from .cycle import ResumeError
from .revise import revise_from_journal
from .journal import Journal, emit
from .muse_client import MuseClient
from .spec import InvalidSpec, RunBudget, RunSpec


def review(spec: RunSpec) -> str:
    run_id = report.begin_run(spec.out_dir, spec, "review")
    j = Journal(Path(spec.out_dir) / "journal.jsonl", run_id=run_id)
    emit(j, "review", "start", spec.question[:200])
    rspec = spec.research_spec()
    with httpx.Client(headers={"User-Agent": "Canary/0.1"}) as http:
        papers = retrieval.retrieve(rspec, http)
    emit(j, "review", "retrieved", f"{len(papers)} candidates")
    report.record_retrieval(spec.out_dir, spec.question, papers)
    if not papers:
        raise RuntimeError("no papers found for this question")
    top = rank.rerank(rspec.question, papers, rspec.max_papers)
    synth = synthesize.synthesize(rspec.question, top, MuseClient(budget=RunBudget.from_spec(spec)))
    emit(j, "review", "synthesized", f"{len(top)} papers, cited {len(synth.cited)}")
    path = report.write_bundle(spec.out_dir, rspec, top, synth, j)
    return str(path / "review.md")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="canary", description="Canary research toolkit.")
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
    lo = sub.add_parser("cycle", help="unattended run: answers become next questions")
    lo.add_argument("question", help="seed research question in plain words")
    lo.add_argument("--csv", default=None, help="optional dataset for analyze iterations")
    lo.add_argument("--target", default=None, help="target column (with --csv)")
    lo.add_argument("--max-iterations", type=int, default=3)
    lo.add_argument("--max-papers", type=int, default=5)
    lo.add_argument("--out", default="./out")
    lo.add_argument("--maintenance", action="store_true", help="assess and patch mid-run and post-run")
    lo.add_argument("--revise-rounds", type=int, default=1)
    lo.add_argument("--max-calls", type=int, default=25, help="hard model-call budget")
    lo.add_argument("--max-tokens", type=int, default=20000000, help="hard token budget")
    lo.add_argument("--wall-time-s", type=int, default=1800, help="hard wall-time budget in seconds")
    lo.add_argument("--repo", default=".", help="repo checkout to revise (container workspace)")
    im = sub.add_parser("revise", help="assess a past run's journal and patch")
    im.add_argument("--run-dir", required=True, help="bundle dir containing journal.jsonl")
    im.add_argument("--repo", default=".", help="repo checkout to revise")
    im.add_argument("--rounds", type=int, default=1)
    im.add_argument("--out", default=None, help="optional dir for assessment.md")
    ins = sub.add_parser("inspect", help="report a bundle's status without executing anything")
    ins.add_argument("bundle", help="bundle directory to inspect")
    rs = sub.add_parser("resume", help="continue an interrupted cycle bundle")
    rs.add_argument("bundle", help="bundle directory to resume")
    rs.add_argument("--fork", default=None, help="continue into a new dir (must not exist)")
    args = ap.parse_args(argv)
    if args.cmd == "inspect":
        print(json.dumps(report.read_bundle(args.bundle), indent=2, default=str))
        return 0
    if args.cmd == "resume":
        try:
            path = resume_bundle(args)
        except ResumeError as e:
            print(f"canary: cannot resume: {e}", file=sys.stderr)
            return 2
        except Exception as e:  # honest failure, never fake output
            print(f"canary: error: {e}", file=sys.stderr)
            return 1
        print(path)
        return 0
    try:
        spec = build_spec(args)
    except InvalidSpec as e:
        print(f"canary: invalid input: {e}", file=sys.stderr)
        return 2
    try:
        if args.cmd == "review":
            path = review(spec)
        elif args.cmd == "analyze":
            path = analyze(spec)
        elif args.cmd == "revise":
            path = revise(args.run_dir, args.repo, args.rounds, args.out)
        else:
            path = cycle(spec, args.repo)
    except Exception as e:  # honest failure, never fake output
        print(f"canary: error: {e}", file=sys.stderr)
        return 1
    print(path)
    return 0


def build_spec(args) -> RunSpec | None:
    """Validate CLI arguments into a RunSpec before any external call."""
    if args.cmd == "revise":
        return None
    if args.cmd == "review":
        return RunSpec(question=args.question, max_papers=args.max_papers,
                       year_from=args.year_from, out_dir=args.out)
    if args.cmd == "analyze":
        question = args.question.strip() or f"what predicts {args.target}?"
        return RunSpec(question=question, csv=args.csv, target=args.target, out_dir=args.out)
    return RunSpec(question=args.question, csv=args.csv, target=args.target,
                   max_iterations=args.max_iterations, max_papers=args.max_papers,
                   maintenance=args.maintenance, revise_rounds=args.revise_rounds,
                   max_model_calls=args.max_calls, max_tokens=args.max_tokens,
                   wall_time_s=args.wall_time_s, out_dir=args.out)


def cycle(spec: RunSpec, repo: str = ".") -> str:
    run_id = report.begin_run(spec.out_dir, spec, "cycle")
    j = Journal(Path(spec.out_dir) / "journal.jsonl", run_id=run_id)
    outbox: dict = {}
    budget = RunBudget.from_spec(spec)
    res = cyclemod.run_cycle(
        spec.question, spec.csv, spec.target, spec.max_iterations, spec.max_papers,
        MuseClient(budget=budget), journal=j, maintenance=spec.maintenance,
        revise_rounds=spec.revise_rounds, repo_root=repo, outbox=outbox,
        spec=spec, budget=budget, record_dir=spec.out_dir,
    )
    path = report.write_cycle_bundle(spec.out_dir, spec.question, res, j, spec)
    if spec.maintenance and outbox.get("revision"):
        _maybe_publish(repo, outbox["revision"], j)
        j.save(path / "journal.jsonl")  # persist publish notes too
    return str(path / "synthesis.md")


def resume_bundle(args) -> str:
    res, journal, spec, out = cyclemod.resume_cycle(args.bundle, MuseClient(), fork_dir=args.fork)
    path = report.write_cycle_bundle(out, spec.question, res, journal, spec)
    return str(path / "synthesis.md")


def _maybe_publish(repo: str, revision, j: Journal) -> None:
    import os

    if not os.environ.get("GITHUB_TOKEN"):
        emit(j, "publish", "skipped", "no GITHUB_TOKEN; local-only")
        print("publish: skipped (no GITHUB_TOKEN)")
        return
    from .github_ops import GitHub, resolve_repo, resolve_token
    from .revise import new_run_id, publish_round

    doc, rep = revision
    gh = GitHub(resolve_token(), resolve_repo(repo))
    pub = publish_round(repo, new_run_id(), doc, rep, j, gh)
    print(f"publish: issue={pub.issue_url or 'none'} pr={pub.pr_url or 'none'} merged={pub.merged}")


def analyze(spec: RunSpec) -> str:
    run_id = report.begin_run(spec.out_dir, spec, "analyze")
    j = Journal(Path(spec.out_dir) / "journal.jsonl", run_id=run_id)
    emit(j, "analyze", "start", f"{spec.csv} target={spec.target}")
    df = datamod.load_csv(spec.csv)
    prep = datamod.prepare(df, spec.target)
    emit(j, "analyze", "prepared", f"{prep.profile.n_rows} rows, {prep.profile.task}")
    res = modeling.run(prep)
    emit(j, "analyze", "modeled", f"{res.best} test={res.best_test} baseline={res.baseline_test}")
    report.record_analysis_inputs(spec.out_dir, spec.question, spec.csv, spec.target, prep, res)
    findings = analysis.narrate(spec.question, prep, res,
                                MuseClient(budget=RunBudget.from_spec(spec)))
    emit(j, "analyze", "narrated", f"warnings={len(res.warnings)}")
    path = report.write_analysis_bundle(spec.out_dir, spec.question, spec.csv, spec.target,
                                        prep, res, findings, j)
    return str(path / "analysis.md")


def revise(run_dir: str, repo: str, rounds: int, out: str | None) -> str:
    jr = Journal.load(Path(run_dir) / "journal.jsonl")
    outcome = f"past run at {run_dir}: {len(jr)} notes"
    for cand in ("run.json", "provenance.json"):
        p = Path(run_dir) / cand
        if p.exists():
            outcome += f"; {cand}={p.read_text(encoding='utf-8')[:800]}"
            break
    doc, rep = revise_from_journal(jr.text(), outcome, repo, MuseClient(), rounds=rounds, journal=jr)
    dest = Path(out or run_dir)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "assessment.md").write_text(doc.markdown + "\n", encoding="utf-8")
    jr.save(dest / "journal.jsonl")
    print(f"revise: kept={rep.kept} reverted={rep.reverted} skipped={rep.skipped}")
    _maybe_publish(repo, (doc, rep), jr)
    jr.save(dest / "journal.jsonl")
    return str(dest / "assessment.md")


if __name__ == "__main__":
    raise SystemExit(main())
