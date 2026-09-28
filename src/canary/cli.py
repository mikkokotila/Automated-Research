"""CLI: question in, cited review out."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import httpx

from . import analysis, data as datamod, cycle as cyclemod, modeling, rank, report, retrieval, synthesize
from .cycle import ResumeError
from . import runs as runsmod
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


def _journal_failure(out_dir: str | None, cmd: str, error: BaseException) -> None:
    """Append a terminal failure event to the run's own journal.

    Best-effort: error reporting must never fail because journaling
    did. Skips sealed history (immutable) and missing journals (the run
    never started). Redaction and fsync come from Journal.note.
    """
    if not out_dir:
        return
    journal_path = Path(out_dir) / "journal.jsonl"
    if not journal_path.is_file():
        return
    try:
        journal = Journal.load(journal_path)
        if any(note.event == "sealed" for note in journal.notes):
            return
        journal.path = journal_path
        if journal.notes:
            journal.run_id = journal.notes[-1].run_id
        journal.note(cmd, "failed", f"{type(error).__name__}: {error}"[:500])
    except Exception:
        pass


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
    lo.add_argument("--profile", default="dev", choices=["dev", "prod"],
                    help="prod runs on main only (dev may use any branch)")
    lo.add_argument("--check-cmd", default=None,
                    help="eval-gate command for revision (default: pytest -q)")
    lo.add_argument("--revise-rounds", type=int, default=1)
    lo.add_argument("--max-calls", type=int, default=25, help="hard model-call budget")
    lo.add_argument("--max-tokens", type=int, default=20000000, help="hard token budget")
    lo.add_argument("--wall-time-s", type=int, default=1800, help="hard wall-time budget in seconds")
    lo.add_argument("--repo", default=".", help="repo checkout to revise (container workspace)")
    lo.add_argument("--assess", action="store_true",
                    help="persist a post-completion assessment (never revises code)")
    lo.add_argument("--bandit-mode", default="off", choices=["off", "fixed", "learning", "frozen"],
                    help="retrieval-strategy experiment: off (default), fixed, learning, frozen")
    lo.add_argument("--bandit-dir", default=None,
                    help="policy dir (required for learning/frozen); learning must live "
                         "inside --out so it exports (e.g. $OUT/bandit); frozen may read "
                         "a staged prior (stage snapshot.json + observations.jsonl, "
                         "then point here at /inputs)")
    lo.add_argument("--bandit-seed", type=int, default=0, help="selector RNG seed")
    lo.add_argument("--bandit-eps", type=float, default=0.1, help="exploration rate 0-0.5")
    im = sub.add_parser("revise", help="assess a past run's journal and patch")
    im.add_argument("--run-dir", required=True, help="bundle dir containing journal.jsonl")
    im.add_argument("--repo", default=".", help="repo checkout to revise")
    im.add_argument("--rounds", type=int, default=1)
    im.add_argument("--profile", default="dev", choices=["dev", "prod"],
                    help="prod runs on main only (dev may use any branch)")
    im.add_argument("--check-cmd", default=None,
                    help="eval-gate command for revision (default: pytest -q)")
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
    pb.add_argument("--run-dir", default=None, help="bundle dir containing journal.jsonl")
    pb.add_argument("--round", default="latest", help="recorded round id to publish")
    pb.add_argument("--bundle", default=None,
                    help="container export bundle to publish from (instead of --run-dir)")
    rn = sub.add_parser("runs", help="named runs: list, inspect, serve, control")
    rnsub = rn.add_subparsers(dest="runs_cmd", required=True)
    rnsub.add_parser("list", help="table of registered runs")
    sh = rnsub.add_parser("show", help="full record for one run")
    sh.add_argument("key", help="registry key")
    lg = rnsub.add_parser("log", help="print a run's raw log")
    lg.add_argument("key", help="registry key")
    lg.add_argument("--tail", type=int, default=200)
    lg.add_argument("--stream", default="all", choices=["all", "timeline", "console"])
    sv = rnsub.add_parser("serve", help="serve the dashboard + control API (foreground)")
    sv.add_argument("--port", type=int, default=None)
    st = rnsub.add_parser("start", help="launch a named run via the daemon")
    st.add_argument("--name", required=True)
    st.add_argument("--brief", default="")
    st.add_argument("--port", type=int, default=None)
    st.add_argument("argv", nargs=argparse.REMAINDER,
                    help="launcher args after -- (must start with the launcher)")
    for verb in ("pause", "unpause"):
        pv = rnsub.add_parser(verb, help=f"{verb} a live run's container")
        pv.add_argument("key", help="registry key")
        pv.add_argument("--port", type=int, default=None)
    rr = rnsub.add_parser("rerun", help="relaunch a run's recorded spec as a new run")
    rr.add_argument("key", help="registry key")
    rr.add_argument("--name", default="")
    rr.add_argument("--brief", default="")
    rr.add_argument("--port", type=int, default=None)
    ad = rnsub.add_parser("adopt", help="register an existing bundle dir")
    ad.add_argument("--bundle", required=True)
    ad.add_argument("--name", default="")
    ad.add_argument("--brief", default="")
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
            _journal_failure(args.bundle, "resume", e)
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
            if args.bundle:
                url = publish_bundle(args.repo, args.bundle)
            elif args.run_dir:
                url = publish_recorded(args.repo, args.run_dir, args.round)
            else:
                print("canary: error: publish needs --bundle or --run-dir",
                      file=sys.stderr)
                return 2
        except Exception as e:
            print(f"canary: error: {e}", file=sys.stderr)
            return 1
        print(url)
        return 0
    if args.cmd == "runs":
        return runs_cmd(args)
    try:
        spec = build_spec(args)
        bandit = build_bandit(args)
        check_cmd = parse_check_cmd(getattr(args, "check_cmd", None))
    except InvalidSpec as e:
        print(f"canary: invalid input: {e}", file=sys.stderr)
        return 2
    try:
        if args.cmd == "review":
            path = review(spec, assess=args.assess)
        elif args.cmd == "analyze":
            path = analyze(spec, assess=args.assess)
        elif args.cmd == "revise":
            path = revise(args.run_dir, args.repo, args.rounds, args.out,
                          profile=args.profile, check_cmd=check_cmd)
        else:
            path = cycle(spec, args.repo, assess=args.assess, bandit=bandit,
                         check_cmd=check_cmd)
    except Exception as e:  # honest failure, never fake output
        owned_out = spec.out_dir if spec is not None else args.out
        _journal_failure(owned_out, args.cmd, e)
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
                   maintenance=args.maintenance, profile=args.profile,
                   revise_rounds=args.revise_rounds,
                   max_model_calls=args.max_calls, max_tokens=args.max_tokens,
                   wall_time_s=args.wall_time_s, out_dir=args.out)


def parse_check_cmd(raw: str | None) -> list[str] | None:
    """Operator-owned eval-gate command. None keeps the pytest default."""
    if raw is None or not raw.strip():
        return None
    try:
        parts = shlex.split(raw)
    except ValueError as e:
        raise InvalidSpec(f"check-cmd is not parseable: {e}") from e
    if not parts:
        return None
    return parts


def build_bandit(args):
    """Validate bandit flags into a config, or None when the experiment is off."""
    from .strategy import BanditConfig

    mode = getattr(args, "bandit_mode", "off")
    if mode == "off":
        return None
    try:
        return BanditConfig(mode=mode, epsilon=args.bandit_eps, seed=args.bandit_seed,
                            policy_dir=args.bandit_dir)
    except (TypeError, ValueError) as e:
        raise InvalidSpec(f"bandit config: {e}") from e


def cycle(spec: RunSpec, repo: str = ".", assess: bool = False, bandit=None,
          check_cmd: list[str] | None = None) -> str:
    run_id = report.begin_run(spec.out_dir, spec, "cycle")
    j = Journal(Path(spec.out_dir) / "journal.jsonl", run_id=run_id)
    outbox: dict = {}
    budget = RunBudget.from_spec(spec)
    client = MuseClient(budget=budget)
    res = cyclemod.run_cycle(
        spec.question, spec.csv, spec.target, spec.max_iterations, spec.max_papers,
        client, journal=j, maintenance=spec.maintenance,
        revise_rounds=spec.revise_rounds, repo_root=repo, outbox=outbox,
        spec=spec, budget=budget, record_dir=spec.out_dir, bandit=bandit,
        check_cmd=check_cmd,
    )
    if assess and not spec.maintenance:  # maintenance already persists assessments
        stopped = getattr(res.stopped, "value", res.stopped)
        _post_assess(j, spec.out_dir, True,
                     f"{len(res.iterations)} iterations, stopped={stopped}", client)
        # Post-assessment spends from the same budget after run_cycle
        # snapshotted usage: refresh so run.json reconciles (Issue #56).
        res = replace(res, usage=budget.usage_summary())
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


def publish_bundle(repo: str, bundle: str) -> str:
    """Maintainer-only bridge: kept guest diffs from an export bundle to PR.

    The worker never calls this: it requires GITHUB_TOKEN, which the worker
    refuses to hold. The bundle is scan-gated and every kept diff is
    verified against its candidate manifest before anything is applied.
    """
    from .github_ops import GitHub, resolve_repo, resolve_token
    from .revise import new_run_id, publish_container_bundle

    jr = Journal.load(Path(bundle) / "journal.jsonl")
    gh = GitHub(resolve_token(), resolve_repo(repo))
    pub = publish_container_bundle(repo, bundle, new_run_id(), jr, gh)
    jr.save(Path(bundle) / "journal.jsonl")
    print(f"publish: issue={pub.issue_url or 'none'} pr={pub.pr_url or 'none'} "
          f"merged={pub.merged}")
    return pub.pr_url or pub.issue_url or ""


def _runs_registry() -> str:
    return os.environ.get("CANARY_RUNS_REGISTRY", str(runsmod.REGISTRY))


def _runs_port(args) -> int:
    if getattr(args, "port", None):
        return args.port
    try:
        return int(os.environ.get("CANARY_RUNS_PORT", "8789"))
    except ValueError:
        return 8789


def _runs_post(port: int, path: str, payload: dict):
    try:
        r = httpx.post(f"http://127.0.0.1:{port}{path}", json=payload, timeout=30.0)
    except httpx.HTTPError:
        return None, ("runsd is not serving on 127.0.0.1:"
                      f"{port} — start it with: canary runs serve --port {port}")
    if r.status_code >= 400:
        try:
            return None, r.json().get("error", f"HTTP {r.status_code}")
        except ValueError:
            return None, f"HTTP {r.status_code}"
    return r.json(), ""


def runs_cmd(args) -> int:
    """Named-runs control surface (local reads + daemon actions)."""
    from . import dashboard as dashboardmod

    cmd = args.runs_cmd
    if cmd == "serve":
        dashboardmod.serve(_runs_port(args))
        return 0
    if cmd == "list":
        rows = runsmod.load(_runs_registry())
        print(f"{'KEY':28} {'STATUS':12} {'KIND':8} NAME")
        for row in rows:
            print(f"{row.get('key', '?'):28} {row.get('status', '?'):12} "
                  f"{row.get('kind', '?'):8} {row.get('name', '')}")
        return 0
    if cmd == "show":
        row = runsmod.get(args.key, _runs_registry())
        if row is None:
            print(f"canary: unknown run {args.key}", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=1))
        return 0
    if cmd == "log":
        row = runsmod.get(args.key, _runs_registry())
        if row is None:
            print(f"canary: unknown run {args.key}", file=sys.stderr)
            return 1
        bundle = row.get("bundle", "")
        if args.stream in ("all", "timeline"):
            for ev in dashboardmod.merged_timeline(bundle, args.tail):
                print(f"{ev['t']} s{ev['seq']} [{ev['source']}:"
                      f"{ev['phase']}/{ev['event']}] {ev['detail']}")
        if args.stream in ("all", "console"):
            console = dashboardmod.console_tail(bundle, args.tail)
            if console["present"]:
                print("--- console ---")
                print("\n".join(console["lines"]))
            else:
                print(f"--- console: {console['note']}")
        return 0
    if cmd == "adopt":
        if not Path(args.bundle).is_dir():
            print(f"canary: not a directory: {args.bundle}", file=sys.stderr)
            return 1
        row = runsmod.adopt_bundle(args.bundle, args.name, args.brief,
                                   _runs_registry())
        print(f"adopted {row['key']}: {row['name']} [{row['status']}]")
        return 0
    port = _runs_port(args)
    if cmd == "start":
        argv = list(args.argv or [])
        if argv and argv[0] == "--":
            argv = argv[1:]
        data, err = _runs_post(port, "/api/runs",
                               {"name": args.name, "brief": args.brief, "argv": argv,
                                "env": runsmod.ambient_launch_env()})
        if err:
            print(f"canary: error: {err}", file=sys.stderr)
            return 1
        print(f"launched {data['key']}")
        return 0
    if cmd in ("pause", "unpause"):
        data, err = _runs_post(port, f"/api/runs/{args.key}/{cmd}", {})
        if err:
            print(f"canary: error: {err}", file=sys.stderr)
            return 1
        print(f"{args.key}: {data['status']}")
        return 0
    if cmd == "rerun":
        data, err = _runs_post(port, f"/api/runs/{args.key}/rerun",
                               {"name": args.name, "brief": args.brief})
        if err:
            print(f"canary: error: {err}", file=sys.stderr)
            return 1
        print(f"relaunched {args.key} as {data['key']}")
        return 0
    print(f"canary: unknown runs command {cmd}", file=sys.stderr)
    return 2


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


def revise(run_dir: str, repo: str, rounds: int, out: str | None,
           profile: str = "dev", check_cmd: list[str] | None = None) -> str:
    from .revise import require_prod_branch

    require_prod_branch(repo, profile)  # prod runs on main only, before any work
    jr = Journal.load(Path(run_dir) / "journal.jsonl")
    outcome = f"past run at {run_dir}: {len(jr)} notes"
    for cand in ("run.json", "provenance.json"):
        p = Path(run_dir) / cand
        if p.exists():
            outcome += f"; {cand}={p.read_text(encoding='utf-8')[:800]}"
            break
    doc, rep = revise_from_journal(jr.text(), outcome, repo, MuseClient(), rounds=rounds,
                                   journal=jr, check_cmd=check_cmd)
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
