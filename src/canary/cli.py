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
from .lifecycle import Lifecycle
from .muse_client import MuseClient, RequestBlocked
from .spec import Cancelled, InvalidSpec, RunBudget, RunSpec


def _post_assess(journal: Journal, out_dir: str, enabled: bool, outcome: str,
                 client: MuseClient) -> None:
    """Auxiliary post-completion assessment: never fails completed research."""
    if not enabled:
        return
    try:
        Lifecycle(journal, out_dir, assess_enabled=True).finish_assessment(
            outcome, client, code_revision=report.code_revision())
    except (RequestBlocked, Cancelled) as e:
        emit(journal, "lifecycle", "assess-aborted",
             f"{type(e).__name__}: {str(e)[:200]}")


def review(spec: RunSpec, assess: bool = False) -> str:
    run_id = report.begin_run(spec.out_dir, spec, "review")
    j = Journal(Path(spec.out_dir) / "journal.jsonl", run_id=run_id)
    emit(j, "review", "start", spec.question[:200])
    rspec = spec.research_spec()
    with httpx.Client(headers={"User-Agent": "Canary/0.1"}) as http:
        papers, retrieval_report = retrieval.retrieve_with_report(rspec, http)
    emit(j, "review", "retrieved", f"{len(papers)} candidates")
    for name, outcome in retrieval_report["providers"].items():
        if outcome["outcome"] != "ok":
            emit(j, "review", "provider-failed", f"{name}: {outcome['error']}")
    warning = retrieval_report["warning"]
    if warning:
        emit(j, "review", "coverage-warning", warning[:300])
    if not papers:
        raise RuntimeError("no papers found for this question")
    ranked = rank.rerank(rspec.question, papers, len(papers))
    report.record_retrieval(spec.out_dir, spec.question, ranked, retrieval_report)
    top = ranked[:rspec.max_papers]
    client = MuseClient(budget=RunBudget.from_spec(spec))
    synth = synthesize.synthesize(rspec.question, top, client)
    emit(j, "review", "synthesized", f"{len(top)} papers, cited {len(synth.cited)}")
    for marker in synth.validation.get("dangling_citations", []):
        emit(j, "review", "dangling-citation", f"[{marker}] points at no paper")
    for problem in synth.validation.get("rejected", []):
        emit(j, "review", "claim-rejected", problem[:250])
    supported = [c for c in synth.claims if c.support in ("supported", "partial")]
    if not synth.cited and not supported:
        raise RuntimeError("retrieved material does not support an answer to this question")
    _post_assess(j, spec.out_dir, assess,
                 f"review: cited {len(synth.cited)}, {len(supported)} grounded claims",
                 client)
    path = report.write_bundle(spec.out_dir, rspec, top, synth, j, warning)
    return str(path / "review.md")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="canary", description="Canary research toolkit.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review", help="run a cited literature review")
    r.add_argument("question", help="research question in plain words")
    r.add_argument("--max-papers", type=int, default=10)
    r.add_argument("--year-from", type=int, default=None)
    r.add_argument("--out", default="./out")
    r.add_argument("--assess", action="store_true",
                   help="persist a post-completion assessment (never revises code)")
    a = sub.add_parser("analyze", help="test a hypothesis against a CSV file")
    a.add_argument("csv", help="path to CSV data file")
    a.add_argument("--target", required=True, help="target column to predict")
    a.add_argument("--question", default="", help="research question in plain words")
    a.add_argument("--out", default="./out")
    a.add_argument("--assess", action="store_true",
                   help="persist a post-completion assessment (never revises code)")
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
    lo.add_argument("--assess", action="store_true",
                    help="persist a post-completion assessment (never revises code)")
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
    rs.add_argument("--expect-revision", default=None,
                    help="continue a restart_required leg research-only on this accepted revision")
    am = sub.add_parser("assess", help="assess a run's journal without changing any code")
    am.add_argument("--run-dir", required=True, help="bundle dir containing journal.jsonl")
    am.add_argument("--out", default=None, help="dir for assessment records (default: run dir)")
    pb = sub.add_parser("publish", help="maintainer-only export of a recorded round")
    pb.add_argument("--repo", default=".", help="repo checkout holding the promotion store")
    pb.add_argument("--run-dir", required=True, help="bundle dir containing journal.jsonl")
    pb.add_argument("--round", default="latest", help="recorded round id to publish")
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
    if args.cmd == "assess":
        try:
            path = assess_only(args.run_dir, args.out)
        except Exception as e:  # honest failure, never fake output
            print(f"canary: error: {e}", file=sys.stderr)
            return 1
        print(path)
        return 0
    if args.cmd == "publish":
        try:
            url = publish_recorded(args.repo, args.run_dir, args.round)
        except Exception as e:
            print(f"canary: error: {e}", file=sys.stderr)
            return 1
        print(url)
        return 0
    try:
        spec = build_spec(args)
    except InvalidSpec as e:
        print(f"canary: invalid input: {e}", file=sys.stderr)
        return 2
    try:
        if args.cmd == "review":
            path = review(spec, assess=args.assess)
        elif args.cmd == "analyze":
            path = analyze(spec, assess=args.assess)
        elif args.cmd == "revise":
            path = revise(args.run_dir, args.repo, args.rounds, args.out)
        else:
            path = cycle(spec, args.repo, assess=args.assess)
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


def cycle(spec: RunSpec, repo: str = ".", assess: bool = False) -> str:
    run_id = report.begin_run(spec.out_dir, spec, "cycle")
    j = Journal(Path(spec.out_dir) / "journal.jsonl", run_id=run_id)
    outbox: dict = {}
    budget = RunBudget.from_spec(spec)
    client = MuseClient(budget=budget)
    res = cyclemod.run_cycle(
        spec.question, spec.csv, spec.target, spec.max_iterations, spec.max_papers,
        client, journal=j, maintenance=spec.maintenance,
        revise_rounds=spec.revise_rounds, repo_root=repo, outbox=outbox,
        spec=spec, budget=budget, record_dir=spec.out_dir,
    )
    if assess and not spec.maintenance:  # maintenance already persists assessments
        stopped = getattr(res.stopped, "value", res.stopped)
        _post_assess(j, spec.out_dir, True,
                     f"{len(res.iterations)} iterations, stopped={stopped}", client)
    path = report.write_cycle_bundle(spec.out_dir, spec.question, res, j, spec,
                                     outbox.get("revision"))
    if spec.maintenance and outbox.get("revision"):
        emit(j, "publish", "deferred",
             "export is a separate maintainer action: run `canary publish` explicitly")
        j.save(path / "journal.jsonl")
    manifest = report.read_bundle(spec.out_dir)["manifest"] or {}
    if manifest.get("status") == "restart_required":
        from .schedule import supervise

        supervise(spec.out_dir)  # fresh-worker legs in new processes
    return str(path / "synthesis.md")


def resume_bundle(args) -> str:
    if getattr(args, "fork", None) and getattr(args, "expect_revision", None):
        raise ResumeError("--fork and --expect-revision are exclusive")
    if getattr(args, "expect_revision", None):
        res, journal, spec, out = cyclemod.resume_after_revision(
            args.bundle, MuseClient(), expect_revision=args.expect_revision)
    else:
        res, journal, spec, out = cyclemod.resume_cycle(args.bundle, MuseClient(),
                                                       fork_dir=args.fork)
    path = report.write_cycle_bundle(out, spec.question, res, journal, spec)
    return str(path / "synthesis.md")


def assess_only(run_dir: str, out: str | None) -> str:
    """Assess a past run's journal. Reads and writes records; changes no code.

    Never calls require_revision_trust: there is nothing to authorize, because
    no patch, subprocess, or tree mutation can happen on this path.
    """
    from .assess import assess_journal
    from .memory import Memory

    jr = Journal.load(Path(run_dir) / "journal.jsonl")
    outcome = f"past run at {run_dir}: {len(jr)} notes"
    for cand in ("run.json", "provenance.json"):
        p = Path(run_dir) / cand
        if p.exists():
            outcome += f"; {cand}={p.read_text(encoding='utf-8')[:800]}"
            break
    dest = Path(out or run_dir) / "assessments"
    memory = Memory(dest / "memory.jsonl")
    record = assess_journal(jr.notes, jr.text(max_chars=60000), outcome, MuseClient(),
                            memory=memory, out_dir=dest,
                            code_revision=report.code_revision())
    jr.save(Path(out or run_dir) / "journal.jsonl")
    print(f"assess: {record.id} status={record.status} "
          f"proposals={len(record.proposals)} calls={record.calls_spent}")
    return str(dest / f"{record.id}.md")


def publish_recorded(repo: str, run_dir: str, round_id: str = "latest") -> str:
    """Maintainer-only export of a recorded revision round. Separate process.

    The worker never calls this: it requires GITHUB_TOKEN, which the worker
    refuses to hold. The round's doc/report are rebuilt from the store record.
    """
    from . import promote as promotemod
    from .github_ops import GitHub, resolve_repo, resolve_token
    from .revise import load_round, new_run_id, publish_round

    store = promotemod.Store(Path(repo) / "runs" / "promotions")
    doc, rep = load_round(store, round_id)
    jr = Journal.load(Path(run_dir) / "journal.jsonl")
    gh = GitHub(resolve_token(), resolve_repo(repo))
    pub = publish_round(repo, new_run_id(), doc, rep, jr, gh)
    jr.save(Path(run_dir) / "journal.jsonl")
    print(f"publish: issue={pub.issue_url or 'none'} pr={pub.pr_url or 'none'} "
          f"merged={pub.merged}")
    return pub.pr_url or pub.issue_url or ""


def analyze(spec: RunSpec, assess: bool = False) -> str:
    run_id = report.begin_run(spec.out_dir, spec, "analyze")
    j = Journal(Path(spec.out_dir) / "journal.jsonl", run_id=run_id)
    emit(j, "analyze", "start", f"{spec.csv} target={spec.target}")
    if analysis.classify_analysis(spec.question) == "causal":
        emit(j, "analyze", "causal-refused", spec.question[:200])
        raise RuntimeError(analysis.CAUSAL_REFUSAL)
    df = datamod.load_csv(spec.csv)
    prep = datamod.prepare(df, spec.target, seed=cyclemod.split_seed(spec.csv, spec.target,
                                                                    spec.question))
    emit(j, "analyze", "prepared", f"{prep.profile.n_rows} rows, {prep.profile.task}")
    res = modeling.run(prep)
    emit(j, "analyze", "modeled", f"{res.best} test={res.best_test} baseline={res.baseline_test}")
    report.record_analysis_inputs(spec.out_dir, spec.question, spec.csv, spec.target, prep, res)
    client = MuseClient(budget=RunBudget.from_spec(spec))
    findings = analysis.narrate(spec.question, prep, res, client)
    emit(j, "analyze", "narrated", f"warnings={len(res.warnings)}")
    _post_assess(j, spec.out_dir, assess,
                 f"analyze: {res.best} test={res.best_test} baseline={res.baseline_test}",
                 client)
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
    emit(jr, "publish", "deferred",
         "export is a separate maintainer action: run `canary publish` explicitly")
    jr.save(dest / "journal.jsonl")
    return str(dest / "assessment.md")


if __name__ == "__main__":
    raise SystemExit(main())
