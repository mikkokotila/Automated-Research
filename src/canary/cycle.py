"""Cycle: answers become next questions until budget or convergence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import analysis, data as datamod, modeling, rank, report, retrieval, synthesize
from .journal import Journal, JournalError, emit
from .muse_client import MuseClient, RequestBlocked
from .schedule import (ScheduleError, Scheduler, SchedulerConfig, Scope, decide)
from .spec import (MAX_ITERATIONS, BudgetExhausted, Cancelled, ResearchSpec, RunBudget,
                   RunSpec, StopReason)
from .synthesize import Completer
FOLLOWUP_SYSTEM = (
    "You are a research strategist. Given the findings so far, propose follow-up "
    "research questions that are answerable and non-redundant. Reply with ONLY a "
    "JSON array of objects with keys: question (string), kind (\"review\" or "
    "\"analyze\"), rationale (one sentence, naming its link to the original "
    "objective), gap (which listed evidence gap this addresses, or \"\"). "
    "Propose from the evidence gaps first; an adjacent question needs an "
    "explicit objective link in its rationale. Prefer \"analyze\" only when the "
    "available dataset plausibly contains the needed variables; otherwise use "
    "\"review\". If nothing worthwhile remains, reply with []. At most 3 items."
)


def propose_prompt(history: str, dataset_hint: str, seed: str, gaps: list[str]) -> str:
    lines = [f"Original objective: {seed}", f"Dataset available: {dataset_hint}",
             "", "Findings so far:", history]
    if gaps:
        lines += ["", "Open evidence gaps:"] + [f"- {g[:300]}" for g in gaps[:8]]
    lines += ["", "Propose follow-ups."]
    return "\n".join(lines)

SYNTHESIS_SYSTEM = (
    "You are a senior researcher. Synthesize the iteration findings below into a "
    "final report: headline answer, supporting evidence per iteration, limits, "
    "and the single most important next question. Under 400 words. Never invent "
    "numbers; cite iterations as (iter N)."
)


class ResumeError(Exception):
    """Raised when a bundle cannot be resumed safely."""


class OpLog:
    """Write-ahead log of billable dispatches.

    Every provider call records a start before the request leaves and a finish
    when any response arrives. At resume, starts with no finish are ambiguous
    (may have run remotely) and are replayed as explicit new attempts.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.open: dict[str, str] = {}

    def start(self, kind: str) -> str:
        op = uuid.uuid4().hex[:12]
        self.open[op] = kind
        self._append({"op": op, "kind": kind, "event": "start",
                      "at": datetime.now(timezone.utc).isoformat()})
        return op

    def finish(self, op: str | None) -> None:
        if op is None:
            return
        self.open.pop(op, None)
        self._append({"op": op, "event": "finish",
                      "at": datetime.now(timezone.utc).isoformat()})

    def mark_checkpoint(self, seq: int) -> None:
        self._append({"event": "checkpoint", "seq": seq,
                      "at": datetime.now(timezone.utc).isoformat()})

    def _append(self, obj: dict) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            raise JournalError(f"ops log not durable: {exc}")

    @staticmethod
    def replay(path: str | Path) -> tuple[list[str], list[str]]:
        """Ops since the last checkpoint marker: (finished, ambiguous)."""
        finished: list[str] = []
        starts: dict[str, str] = {}
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            return finished, []
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "checkpoint":
                starts = {}
                finished = []
            elif event.get("event") == "start":
                starts[event.get("op", "?")] = event.get("kind", "?")
            elif event.get("event") == "finish":
                op = event.get("op")
                if op in starts:
                    finished.append(f"{starts.pop(op)}:{op}")
        ambiguous = [f"{kind}:{op}" for op, kind in starts.items()]
        return finished, ambiguous


@dataclass(frozen=True)
class Followup:
    question: str
    kind: str  # "review" | "analyze"
    rationale: str
    gap: str = ""  # evidence gap this question addresses, in the proposer's words
    parent: str = ""  # "seed" or "iterN": set by the runner, never the model


@dataclass
class Iteration:
    n: int
    question: str
    kind: str
    summary: str  # short text for next-round context
    detail: str  # full markdown written to disk
    provenance: dict = field(default_factory=dict)


def _parse_followups(text: str) -> tuple[list[Followup], str]:
    """Parse model-proposed follow-ups; diagnosis in {"ok", "empty", "malformed"}."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return [], "malformed"
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return [], "malformed"
    if not isinstance(raw, list):
        return [], "malformed"
    if raw == []:
        return [], "empty"
    out: list[Followup] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        q = str(item.get("question", "")).strip()
        kind = str(item.get("kind", "")).strip().lower()
        if not q or kind not in ("review", "analyze"):
            continue
        out.append(Followup(question=q[:500], kind=kind,
                            rationale=str(item.get("rationale", ""))[:300],
                            gap=str(item.get("gap", ""))[:300]))
    return out[:3], ("ok" if out else "malformed")


def parse_followups(text: str) -> list[Followup]:
    """Parse Muse's JSON; anything unparseable means stop (never crash)."""
    return _parse_followups(text)[0]


def diagnose_followups(text: str) -> str:
    """Classify a propose reply: "ok", "empty" (converged), or "malformed"."""
    return _parse_followups(text)[1]


def replace_followup_parent(f: Followup, parent: str) -> Followup:
    """Bind lineage runner-side; the model never sets its own parent."""
    return replace(f, parent=parent)


def _harvest_gaps(it: Iteration) -> list[str]:
    """Unresolved claims become the next propose round's evidence gaps."""
    gaps = []
    for claim in it.provenance.get("claims", []):
        if claim.get("support") in ("unsupported", "contradicted", "partial"):
            gaps.append(f"iter{it.n}/{claim.get('id')}: {claim.get('text', '')[:200]} "
                        f"({claim.get('support')})")
    return gaps


def propose(history: str, dataset_hint: str, client: Completer, seed: str = "",
            gaps: list[str] | None = None) -> list[Followup]:
    user = propose_prompt(history, dataset_hint, seed or history[:200], gaps or [])
    return parse_followups(client.complete(FOLLOWUP_SYSTEM, user))


class NoEvidence(RuntimeError):
    """A search completed but found nothing usable."""


def run_review(question: str, max_papers: int, http: httpx.Client, muse: Completer, journal: Journal | None = None,
               record_dir: str | Path | None = None, selector=None,
               budget_frac: float = 1.0) -> Iteration:
    """One review step. A bandit selector swaps the retrieval query only;
    rerank, synthesis, and scoring stay anchored to the original question.
    The session degrades to fixed internally on store failure, loudly."""
    emit(journal, "review", "start", question[:200])
    goal = question
    decision = selector.decide(goal, budget_frac=budget_frac, model=muse.model) \
        if selector is not None else None
    spec = ResearchSpec(question=decision.query if decision else question,
                        max_papers=max_papers)
    papers, retrieval_report = retrieval.retrieve_with_report(spec, http)
    emit(journal, "review", "retrieved", f"{len(papers)} candidates")
    for name, outcome in retrieval_report["providers"].items():
        if outcome["outcome"] != "ok":
            emit(journal, "review", "provider-failed", f"{name}: {outcome['error']}")
    for change in retrieval_report["refinements"]:
        emit(journal, "review", "query-refined", f"{change['from'][:100]} -> {change['to'][:100]}")
    warning = retrieval_report["warning"]
    if warning:
        emit(journal, "review", "coverage-warning", warning[:300])
    if not papers:
        if decision is not None and selector is not None:
            selector.score_and_observe(decision, {
                "question": goal, "papers": [], "claims": (),
                "validation": {}, "dedupe": retrieval_report["dedupe"],
                "n_provider_calls": 2 * len(retrieval_report["queries"]),
                "n_model_calls": 0,
                "providers_ok": any(p["outcome"] == "ok"
                                    for p in retrieval_report["providers"].values())})
        raise NoEvidence("no papers found for this question")
    ranked = rank.rerank(goal, papers, len(papers))
    if record_dir is not None:
        report.record_retrieval(record_dir, goal, ranked, retrieval_report)
    top = ranked[:spec.max_papers]
    synth = synthesize.synthesize(goal, top, muse, warning)
    emit(journal, "review", "synthesized", f"{len(top)} papers, cited {len(synth.cited)}")
    if warning and not synthesize.has_coverage_footer(synth.text):
        emit(journal, "review", "coverage-footer-missing",
             "model omitted the incomplete-coverage footer")
    elif synth.validation.get("footer_appended"):
        emit(journal, "review", "coverage-footer-appended",
             "model omitted the footer; guard appended it")
    for marker in synth.validation.get("dangling_citations", []):
        emit(journal, "review", "dangling-citation", f"[{marker}] points at no paper")
    for problem in synth.validation.get("rejected", []):
        emit(journal, "review", "claim-rejected", problem[:250])
    supported = [c for c in synth.claims if c.support in ("supported", "partial")]
    if not synth.cited and not supported:
        if decision is not None and selector is not None:
            selector.score_and_observe(decision, {
                "question": goal, "papers": ranked, "claims": synth.claims,
                "validation": synth.validation,
                "dedupe": retrieval_report["dedupe"],
                "n_provider_calls": 2 * len(retrieval_report["queries"]),
                "n_model_calls": 1,
                "providers_ok": any(p["outcome"] == "ok"
                                    for p in retrieval_report["providers"].values())})
        raise NoEvidence("retrieved material does not support an answer to this question")
    detail = report.render_markdown(spec, top, synth, warning)
    cited = ", ".join(f"[{i}]" for i in synth.cited[:6]) or "none"
    summary = f"Q: {question}\nReview of {len(top)} papers (cited {cited}). {synth.text[:800]}"
    caveats = "; ".join(f"{c.id} {c.support}: {c.uncertainty or c.text}"[:200]
                        for c in synth.claims if c.support != "supported" or c.uncertainty)
    if caveats:
        summary += f"\nCaveats: {caveats[:600]}"
    prov = {"kind": "review", "papers": [p.title for p in top], "model": synth.model,
            "coverage_warning": warning, "claims": [asdict(c) for c in synth.claims],
            "validation": synth.validation}
    if decision is not None and selector is not None:
        obs = selector.score_and_observe(decision, {
            "question": goal, "papers": ranked, "claims": synth.claims,
            "validation": synth.validation,
            "dedupe": retrieval_report["dedupe"],
            "n_provider_calls": 2 * len(retrieval_report["queries"]),
            "n_model_calls": 1,
            "providers_ok": any(p["outcome"] == "ok"
                                for p in retrieval_report["providers"].values())})
        prov["strategy"] = {"decision_id": decision.id, "strategy_id": decision.strategy_id,
                            "query": decision.query, "fallback": decision.fallback,
                            "mode": decision.mode, "reward_status": obs["status"],
                            "reward": obs["reward"]}
    return Iteration(n=0, question=question, kind="review", summary=summary, detail=detail, provenance=prov)


def split_seed(csv: str, target: str, question: str) -> int:
    """Stable per-question split seed: reproducibility without one global holdout.

    Identical (csv, target, question) replays the same split; anything else
    rotates the holdout, so an adaptive cycle cannot repeatedly optimize
    against one disclosed outcome. The seed is recorded in provenance.
    """
    digest = hashlib.sha256(f"{csv}\n{target}\n{question}".encode()).hexdigest()
    return int(digest[:8], 16)


def run_analyze(question: str, csv: str, target: str, muse: Completer, journal: Journal | None = None,
                record_dir=None) -> Iteration:
    emit(journal, "analyze", "start", f"{csv} target={target}")
    if analysis.classify_analysis(question) == "causal":
        emit(journal, "analyze", "causal-refused", question[:200])
        raise NoEvidence(analysis.CAUSAL_REFUSAL)
    df = datamod.load_csv(csv)
    prep = datamod.prepare(df, target, seed=split_seed(csv, target, question))
    emit(journal, "analyze", "prepared", f"{prep.profile.n_rows} rows, {prep.profile.task}")
    res = modeling.run(prep)
    emit(journal, "analyze", "modeled", f"{res.best} test={res.best_test} baseline={res.baseline_test}")
    if record_dir is not None:
        report.record_analysis_inputs(record_dir, question, csv, target, prep, res)
    findings = analysis.narrate(question, prep, res, muse)
    emit(journal, "analyze", "narrated", f"warnings={len(res.warnings)}")
    detail = report.render_analysis(question, csv, target, prep, res, findings)
    summary = f"Q: {question}\n{res.task} on {prep.profile.n_rows} rows: {res.best} test {res.best_test} vs baseline {res.baseline_test}. {findings.text[:600]}"
    prov = {
        "kind": "analyze",
        "analysis_kind": res.kind,
        "split_seed": prep.seed,
        "csv": csv,
        "target": target,
        "best": res.best,
        "best_test": res.best_test,
        "baseline_test": res.baseline_test,
        "warnings": list(res.warnings),
        "model": findings.model,
    }
    return Iteration(n=0, question=question, kind="analyze", summary=summary, detail=detail, provenance=prov)


@dataclass(frozen=True)
class CycleResult:
    iterations: tuple[Iteration, ...]
    synthesis: str
    model: str
    stopped: StopReason | str
    unanswered: tuple[str, ...] = ()  # proposed but never executed
    usage: dict = field(default_factory=dict)  # exact calls/tokens; see tokens_reported


@dataclass(frozen=True)
class MidRunOutcome:
    """What one interleaved revise pass did. assessed=False means it errored first."""

    kept: int = 0
    attempted: bool = False
    assessed: bool = True


def _iteration_supported(it: Iteration) -> bool:
    """Grounded output counts as progress; marker-only citations do not.

    A completed analysis is grounded in computed metrics (failures raise
    instead of completing). A review needs at least one supported or partial
    claim — bare [n] markers without a validated claims block are ungrounded.
    """
    if it.kind == "analyze":
        return True
    return any(c.get("support") in ("supported", "partial")
               for c in it.provenance.get("claims", []))


def _accepted_rev(repo_root: str | None) -> str | None:
    try:
        from . import promote as promotemod

        return promotemod.Store(Path(repo_root) / "runs" / "promotions").latest_rev()
    except Exception:
        return None


class BudgetedCompleter:
    """Wrap a bare Completer so every call spends the shared run budget."""

    def __init__(self, inner: Completer, budget: RunBudget,
                 recorder: OpLog | None = None) -> None:
        self._inner = inner
        self._budget = budget
        self.recorder = recorder

    @property
    def model(self):
        return self._inner.model

    def complete(self, system: str, user: str, max_tokens: int = 8000) -> str:
        self._budget.reserve_call()
        op = self.recorder.start("complete") if self.recorder else None
        text = self._inner.complete(system, user, max_tokens)
        if self.recorder:  # no finish on failure: the outcome is unknown
            self.recorder.finish(op)
        return text


def normalize(question: str) -> str:
    return re.sub(r"\W+", " ", question.lower()).strip()


def run_cycle(
    question: str,
    csv: str | None,
    target: str | None,
    max_iterations: int,
    max_papers: int,
    muse: Completer,
    http: httpx.Client | None = None,
    journal: Journal | None = None,
    maintenance: bool = False,
    revise_rounds: int = 1,
    repo_root: str | None = None,
    check_cmd: list[str] | None = None,
    outbox: dict | None = None,
    spec: RunSpec | None = None,
    budget: RunBudget | None = None,
    record_dir: str | Path | None = None,
    resume: dict | None = None,
    bandit=None,
) -> CycleResult:
    resumed = resume or {}
    if spec is None:  # explicit spec wins; resume carries its own; else legacy args
        spec = (RunSpec.from_dict(resumed["spec"]) if resumed else
                RunSpec(question=question, csv=csv, target=target, max_papers=max_papers,
                        maintenance=maintenance, max_iterations=max_iterations,
                        revise_rounds=revise_rounds))
    if maintenance and repo_root is not None:
        from .revise import (refuse_maintainer_credentials, require_prod_branch,
                             require_revision_trust)

        refuse_maintainer_credentials()  # no export credentials in the worker
        require_revision_trust()  # fail the maintained run before any work
        require_prod_branch(repo_root, spec.profile)  # prod runs on main only
    if budget is None:
        budget = (RunBudget.from_dict(resumed["budget"]) if resumed else
                  getattr(muse, "budget", None) or RunBudget.from_spec(spec))
    scope = Scope(maintenance=maintenance, repo_root=repo_root,
                  check_cmd=tuple(check_cmd) if check_cmd else None,
                  revise_rounds=spec.revise_rounds,
                  bandit=bandit.to_dict() if bandit is not None else None,
                  profile=spec.profile)
    if resumed and resumed.get("scope") is not None:
        try:
            ckpt_scope = Scope.from_dict(resumed["scope"])
        except ScheduleError as e:
            raise ResumeError(f"checkpoint scope unusable: {e}") from e
        if ckpt_scope != scope:  # goal/approval scope is frozen, never steered
            raise ResumeError(
                f"scope mismatch: checkpoint={ckpt_scope.to_dict()} args={scope.to_dict()}")
    revising = maintenance and repo_root is not None
    selector = None
    if bandit is not None and bandit.mode != "off":
        from .strategy import SelectorSession

        selector = SelectorSession(bandit, journal, record_dir,
                                   journal.run_id if journal else "run")
    if resumed and resumed.get("scheduler") is not None:
        try:
            sched = Scheduler.from_dict(resumed["scheduler"])
        except ScheduleError as e:
            raise ResumeError(f"checkpoint scheduler unusable: {e}") from e
        sched.scope = scope  # counters carry; envelope rebinds to this leg
        sched.config = replace(sched.config, allow_revision=revising)
    else:
        sched = Scheduler(config=SchedulerConfig(allow_revision=revising), scope=scope)
        if resumed:
            emit(journal, "resume", "checkpoint-migrated",
                 "v1 checkpoint predates scheduler state; counters start at zero")
    restart_pending: str | None = None
    oplog = OpLog(Path(record_dir) / "ops.jsonl" if record_dir else None)
    if isinstance(muse, MuseClient):
        muse.budget = budget  # rebind: exactly one shared tracker per run
        muse.recorder = oplog
    else:
        muse = BudgetedCompleter(muse, budget, oplog)
    own = http is None
    http = http or httpx.Client(headers={"User-Agent": "Canary/0.1"})
    if resumed:
        iterations = [Iteration(**i) for i in resumed["iterations"]]
        pending = [Followup(**f) for f in resumed["pending"]]
        seen = set(resumed["seen"])
        failed_q = resumed["failed_q"]
        next_seq = resumed["seq"] + 1
        code_hash, csv_hash = resumed["code_hash"], resumed["csv_hash"]
    else:
        iterations = []
        pending = [Followup(question=question, kind="review", rationale="seed")]
        if csv and target:
            pending.append(Followup(question=f"what predicts {target}?", kind="analyze",
                                    rationale="seed"))
        seen = {normalize(j.question) for j in pending}
        failed_q = None
        next_seq = 0
        code_hash, csv_hash = report.code_revision(), report.file_hash(spec.csv)
    stopped = StopReason.CONVERGED
    bad_proposes = 0
    evidence_gaps = [g for it in iterations for g in _harvest_gaps(it)]
    synthesis = ""
    if maintenance and repo_root is None:
        emit(journal, "cycle", "revise-disabled", "maintenance needs repo_root")
    emit(journal, "cycle", "start", f"seed={question[:150]} max_iter={spec.max_iterations}")

    def _checkpoint() -> None:
        nonlocal next_seq
        if record_dir is None:
            return
        state = {"seq": next_seq, "run_id": journal.run_id if journal else None,
                 "spec": spec.to_dict(), "code_hash": code_hash, "csv_hash": csv_hash,
                 "repo_root": repo_root, "check_cmd": check_cmd,
                 "iterations": [asdict(i) for i in iterations],
                 "pending": [asdict(f) for f in pending], "seen": sorted(seen),
                 "failed_q": failed_q, "budget": budget.to_dict(),
                 "scheduler": sched.to_dict(), "scope": scope.to_dict(),
                 "restart_pending": restart_pending,
                 "journal_len": len(journal) if journal else 0}
        report.write_checkpoint(record_dir, state)
        oplog.mark_checkpoint(next_seq)
        next_seq += 1

    try:
        _checkpoint()  # seq 0: even a crashed-first-iteration run resumes
        while len(iterations) < spec.max_iterations:
            budget.check()  # refuse new dispatch at every step
            if not pending:  # propose only once queued work drains
                history = "\n\n---\n\n".join(f"[iter {i.n}] {i.summary}" for i in iterations)
                hint = f"{csv} (target {target})" if csv else "none — reviews only"
                diagnosis = "malformed"
                followups: list[Followup] = []
                for attempt in range(2):  # malformed proposals get one retry
                    try:
                        reply = muse.complete(
                            FOLLOWUP_SYSTEM,
                            propose_prompt(history, hint, question, evidence_gaps))
                    except (RequestBlocked, BudgetExhausted, Cancelled):
                        raise
                    except Exception as e:
                        emit(journal, "cycle", "propose-failed",
                             f"{type(e).__name__}: {str(e)[:200]}")
                        break
                    followups, diagnosis = _parse_followups(reply)
                    if diagnosis != "malformed":
                        break
                    emit(journal, "cycle", "propose-malformed", f"attempt={attempt + 1}")
                if not csv:  # no dataset: only reviews are executable
                    followups = [f for f in followups if f.kind == "review"]
                    if diagnosis == "ok" and not followups:
                        diagnosis = "empty"
                if diagnosis == "malformed":
                    bad_proposes += 1
                    if bad_proposes >= 2:  # provider speaks no usable JSON: stop honestly
                        stopped = StopReason.FAILED
                        emit(journal, "cycle", "propose-unusable",
                             "two consecutive malformed propose rounds")
                        break
                    continue  # re-propose once more; pending is empty so this decides now
                bad_proposes = 0
                fresh: list[Followup] = []
                parent = f"iter{len(iterations)}" if iterations else "seed"
                for f in followups:
                    key = normalize(f.question)
                    if key not in seen:
                        seen.add(key)
                        fresh.append(replace_followup_parent(f, parent))
                        emit(journal, "cycle", "proposed",
                             f"{f.question[:120]} parent={parent} gap={f.gap[:120]}")
                if not fresh:  # nothing new: converged is honest only on a clean []
                    if diagnosis == "empty":
                        stopped = StopReason.CONVERGED
                    else:  # reproposed only known questions: novelty is exhausted
                        stopped = StopReason.REPEATED_QUESTION
                        emit(journal, "cycle", "repeated-question",
                             f"{len(followups)} proposals, all already seen")
                    break
                pending.extend(fresh)
                _checkpoint()  # proposed work is durable before any of it runs
            if pending and budget.calls_remaining() <= spec.finalize_calls:
                stopped = StopReason.BUDGET_EXHAUSTED  # hold back finalization
                emit(journal, "cycle", "finalize-reserve",
                     f"holding {spec.finalize_calls} calls for the final report")
                break
            job = pending.pop(0)
            try:
                if job.kind == "analyze" and csv and target:
                    it = run_analyze(job.question, csv, target, muse, journal, record_dir)
                else:
                    if selector is not None:
                        selector.set_gaps(evidence_gaps)
                    budget_frac = budget.calls_remaining() / max(budget.max_calls, 1)
                    it = run_review(job.question, spec.max_papers, http, muse, journal,
                                    record_dir, selector, budget_frac)
            except (RequestBlocked, BudgetExhausted, Cancelled):
                raise
            except NoEvidence as e:
                failed_q = job.question
                stopped = StopReason.FAILED if iterations else StopReason.INSUFFICIENT_EVIDENCE
                emit(journal, "cycle", "no-evidence", str(e)[:200])
                break
            except Exception as e:
                stopped = StopReason.FAILED
                failed_q = job.question
                emit(journal, "cycle", "failed", str(e)[:300])
                break
            it.n = len(iterations) + 1
            iterations.append(it)
            if record_dir is not None:
                report.write_iteration(record_dir, it)
            _checkpoint()  # iterations, pending, seen, budgets — all durable
            bad_proposes = 0
            evidence_gaps.extend(_harvest_gaps(it))
            emit(journal, "cycle", "iter-done", f"n={it.n} kind={it.kind}")
            sched.note_iteration(_iteration_supported(it), len(evidence_gaps))
            decision = decide(sched)
            emit(journal, "schedule", "decide", f"{decision.action}: {decision.reason}")
            if decision.action == "stop":
                stopped = StopReason(decision.reason)
                emit(journal, "cycle", stopped.value.replace("_", "-"),
                     f"scheduler stop: {decision.reason} "
                     f"(no_progress={sched.no_progress})")
                break
            if decision.action == "assess":  # interleaved maintenance, then maybe restart
                calls_before = budget.calls
                mid = _mid_run_revise(
                    journal, it, repo_root, muse, check_cmd, budget,
                    str(Path(record_dir) / "assessments") if record_dir else None)
                sched.note_revision(mid.kept, mid.attempted,
                                    budget.calls - calls_before, mid.assessed)
                if mid.kept > 0 and sched.config.restart_on_revision:
                    # The tree changed under already-imported modules: halt for a
                    # fresh worker. Continuing here would run stale code silently.
                    restart_pending = _accepted_rev(repo_root)
                    if restart_pending is None:
                        stopped = StopReason.FAILED
                        emit(journal, "cycle", "restart-unknown",
                             "accepted a patch but no accepted revision is recorded")
                        break
                    emit(journal, "cycle", "restart-required",
                         f"accepted {restart_pending}; halting for a fresh worker")
                    _checkpoint()
                    unanswered = tuple(j.question for j in pending)
                    return CycleResult(tuple(iterations), "", muse.model,
                                       StopReason.RESTART_REQUIRED, unanswered,
                                       budget.usage_summary())
            if len(iterations) >= spec.max_iterations:
                stopped = StopReason.BUDGET_EXHAUSTED if pending else StopReason.CONVERGED
        if not iterations:
            if failed_q is None:  # dry start: converged on nothing is not convergence
                stopped = StopReason.INSUFFICIENT_EVIDENCE
            unanswered = tuple(([failed_q] if failed_q else []) + [j.question for j in pending])
            emit(journal, "cycle", "done", f"iters=0 stopped={stopped.value}")
            return CycleResult((), "", muse.model, stopped, unanswered, budget.usage_summary())
        unanswered = tuple(([failed_q] if failed_q else []) + [j.question for j in pending])
        history = "\n\n---\n\n".join(f"[iter {i.n}] {i.summary}" for i in iterations)
        tail = f"\n\nUnanswered (budget ran out, carry forward): {list(unanswered)}" if unanswered else ""
        synthesis = muse.complete(SYNTHESIS_SYSTEM, f"Iterations:\n{history}{tail}")
        if not synthesis.strip():
            stopped = StopReason.FAILED
            synthesis = ""
            emit(journal, "cycle", "empty-synthesis", "final synthesis was empty")
        else:
            emit(journal, "cycle", "done",
                 f"iters={len(iterations)} stopped={stopped.value}")
        if revising:
            veto = sched.revision_veto()
            if veto is not None:
                emit(journal, "schedule", "revise-vetoed", veto)
            else:
                try:
                    from .revise import revise_from_journal

                    calls_before = budget.calls
                    outcome = f"{len(iterations)} iterations, stopped={stopped.value}"
                    assess_dir = str(Path(record_dir) / "assessments") if record_dir else None
                    doc, rep = revise_from_journal(
                        journal.text() if journal else "", outcome, repo_root, muse,
                        rounds=spec.revise_rounds, check_cmd=check_cmd, journal=journal,
                        assess_dir=assess_dir, budget=budget,
                    )
                    sched.note_revision(rep.kept, rep.kept + rep.reverted > 0,
                                        budget.calls - calls_before)
                    emit(journal, "cycle", "revised", f"kept={rep.kept} reverted={rep.reverted} skipped={rep.skipped}")
                    if outbox is not None:
                        outbox["revision"] = (doc, rep)
                except (RequestBlocked, BudgetExhausted, Cancelled):
                    raise
                except Exception as e:
                    emit(journal, "cycle", "revise-failed", str(e)[:200])
        return CycleResult(tuple(iterations), synthesis, muse.model, stopped,
                           unanswered, budget.usage_summary())
    except BudgetExhausted as e:
        if not iterations:
            raise
        stopped = StopReason.BUDGET_EXHAUSTED
        emit(journal, "cycle", "budget-exhausted", str(e)[:200])
    except Cancelled as e:
        if not iterations:
            raise
        stopped = StopReason.CANCELLED
        emit(journal, "cycle", "cancelled", str(e)[:200])
    except RequestBlocked as e:
        if not iterations:
            raise
        stopped = StopReason.PROVIDER_BLOCKED
        emit(journal, "cycle", "provider-blocked", str(e)[:200])
    except KeyboardInterrupt:
        if not iterations:
            raise
        stopped = StopReason.CANCELLED
        emit(journal, "cycle", "cancelled", "interrupted")
    except Exception as e:
        if not iterations and failed_q is None:
            raise
        stopped = StopReason.FAILED
        emit(journal, "cycle", "failed", str(e)[:300])
    finally:
        if own:
            http.close()
    unanswered = tuple(([failed_q] if failed_q else []) + [j.question for j in pending])
    return CycleResult(tuple(iterations), synthesis, muse.model, stopped,
                       unanswered, budget.usage_summary())


def _mid_run_revise(journal: Journal | None, it: Iteration, repo_root: str | None, muse: Completer,
                    check_cmd: list[str] | None, budget=None,
                    assess_dir: str | None = None) -> MidRunOutcome:
    """One bounded revise pass on the latest iteration's notes. Never raises.

    Kept patches land on disk and are validated, but this process keeps
    running the already-imported modules: the caller must halt for a fresh
    worker (RESTART_REQUIRED) instead of continuing on stale imports.
    """
    if journal is None or repo_root is None:
        return MidRunOutcome(assessed=False)
    try:
        from .revise import revise_from_journal

        _, rep = revise_from_journal(
            journal.text(), f"mid-run after iter {it.n} ({it.kind})", repo_root, muse,
            rounds=1, check_cmd=check_cmd, journal=journal, assess_dir=assess_dir,
            budget=budget,
        )
        emit(journal, "cycle", "mid-revised", f"iter={it.n} kept={rep.kept}")
        return MidRunOutcome(kept=rep.kept, attempted=rep.kept + rep.reverted > 0)
    except (RequestBlocked, BudgetExhausted, Cancelled):
        raise
    except Exception as e:
        emit(journal, "cycle", "mid-revise-failed", str(e)[:200])
        return MidRunOutcome(assessed=False)


def _copy_history(src: str | Path, dst: str | Path) -> None:
    """Copy finished history into a fresh fork target; the old manifest stays behind."""
    src, dst = Path(src), Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("journal.jsonl", "ops.jsonl", "retrieval.jsonl", "analysis.jsonl"):
        origin = src / name
        if origin.exists():
            shutil.copy2(origin, dst / name)
    for name in ("iterations", "checkpoints", "assessments"):
        origin = src / name
        if origin.is_dir():
            shutil.copytree(origin, dst / name)


_CHECKPOINT_KEYS = ("spec", "iterations", "pending", "seen", "failed_q", "seq",
                    "budget", "code_hash", "csv_hash")


def _require_checkpoint_keys(checkpoint: dict) -> None:
    missing = [k for k in _CHECKPOINT_KEYS if k not in checkpoint]
    if missing:
        raise ResumeError(f"checkpoint schema incompatible, missing {missing}: "
                          "start a new run; archived history is preserved")


def _open_resume_journal(target: Path, run_id: str, min_len: int) -> Journal:
    journal, journal_report = Journal.inspect(target / "journal.jsonl")
    if journal_report["corrupt_lines"]:
        raise ResumeError(f"journal has corrupt lines {journal_report['corrupt_lines']}: "
                          "refusing unsafe resume")
    journal.path = target / "journal.jsonl"
    journal.run_id = run_id
    if len(journal) < min_len:
        raise ResumeError("journal shorter than the checkpoint: refusing unsafe resume")
    return journal


def resume_cycle(bundle_dir: str | Path, muse: Completer,
                 http: httpx.Client | None = None,
                 fork_dir: str | Path | None = None,
                 ) -> tuple[CycleResult, Journal, RunSpec, str]:
    """Continue an interrupted cycle bundle in place, or fork it explicitly.

    In place: worker code and inputs must hash-match the checkpoint. Fork:
    history is copied, budgets restart, and the new manifest names its parent.
    v1 checkpoints migrate explicitly (scheduler counters reset, journaled);
    anything else incompatible halts with the reason named.
    """
    bundle = report.read_bundle(bundle_dir)
    manifest = bundle["manifest"]
    if manifest is None:
        if bundle["status"] == "legacy":
            raise ResumeError("legacy bundle has no manifest: inspect it, then start a new run")
        raise ResumeError(f"cannot resume a {bundle['status']} bundle")
    if manifest.get("kind") != "cycle":
        raise ResumeError("resume supports cycle runs only; rerun single-shot commands explicitly")
    checkpoint = report.read_checkpoints(bundle_dir)
    if checkpoint is None:
        raise ResumeError("no checkpoint committed: nothing to resume; start a new run")
    _require_checkpoint_keys(checkpoint)
    spec = RunSpec.from_dict(checkpoint["spec"])
    forking = fork_dir is not None
    if manifest.get("status") != "in_progress" and not forking:
        raise ResumeError(f"run already {manifest['status']}: pass --fork to continue as a fork")
    code_now, csv_now = report.code_revision(), report.file_hash(spec.csv)
    if forking:
        target = Path(fork_dir)
        if target.exists():
            raise ResumeError("fork target must not exist")
        _copy_history(bundle_dir, target)
        run_id = report.begin_run(target, spec, "cycle", forked_from=manifest["run_id"])
        resume_budget = RunBudget.from_spec(spec).to_dict()  # spends never transfer
    else:
        if checkpoint["code_hash"] != code_now:
            raise ResumeError("worker code changed since the checkpoint: pass --fork to fork explicitly")
        if checkpoint["csv_hash"] != csv_now:
            raise ResumeError("inputs changed since the checkpoint: pass --fork to fork explicitly")
        target = Path(bundle_dir)
        run_id = manifest["run_id"]
        resume_budget = checkpoint["budget"]
    journal = _open_resume_journal(target, run_id, checkpoint.get("journal_len", 0))
    finished, ambiguous = OpLog.replay(target / "ops.jsonl")
    kept = f"from checkpoint {checkpoint['seq']} ({len(checkpoint['iterations'])} iterations kept)"
    journal.trusted_note("resume", "continued",
                         kept + (f", forked from {manifest['run_id']}" if forking else ""))
    for op in ambiguous:
        journal.trusted_note("resume", "ambiguous-op",
                             f"{op} has no recorded response; replaying as a new attempt")
    if finished:
        journal.trusted_note("resume", "uncheckpointed-ops",
                             f"{len(finished)} call(s) finished after the last checkpoint "
                             "marker; replaying only what the checkpoint lacks")
    resume_state = {"spec": checkpoint["spec"], "iterations": checkpoint["iterations"],
                    "pending": checkpoint["pending"], "seen": checkpoint["seen"],
                    "failed_q": checkpoint["failed_q"], "seq": checkpoint["seq"],
                    "budget": resume_budget,
                    "scheduler": checkpoint.get("scheduler"),
                    "scope": checkpoint.get("scope"),
                    "code_hash": code_now if forking else checkpoint["code_hash"],
                    "csv_hash": csv_now if forking else checkpoint["csv_hash"]}
    result = run_cycle(spec.question, spec.csv, spec.target, spec.max_iterations,
                       spec.max_papers, muse, http, journal=journal,
                       maintenance=spec.maintenance, revise_rounds=spec.revise_rounds,
                       repo_root=checkpoint.get("repo_root"), check_cmd=checkpoint.get("check_cmd"),
                       spec=spec, record_dir=str(target), resume=resume_state,
                       bandit=_bandit_from_checkpoint(checkpoint))
    return result, journal, spec, str(target)


def _bandit_from_checkpoint(checkpoint: dict):
    """Rebuild the bandit config from checkpoint scope. Invalid blocks halt."""
    from .strategy import BanditConfig

    scope = checkpoint.get("scope") or {}
    block = scope.get("bandit")
    if block is None:
        return None
    try:
        return BanditConfig.from_dict(block)
    except (TypeError, ValueError) as exc:
        raise ResumeError(f"checkpoint bandit block unusable: {exc}") from exc


def resume_after_revision(bundle_dir: str | Path, muse: Completer,
                          http: httpx.Client | None = None,
                          expect_revision: str = "",
                          ) -> tuple[CycleResult, Journal, RunSpec, str]:
    """Continue a restart_required leg in a fresh worker on the new revision.

    Verifies the checkpoint waited for exactly expect_revision AND the
    promotion store's accepted revision matches it, then re-anchors the code
    hash explicitly (the tree legitimately changed) and restores compatible
    checkpoint state: iterations, pending work, budgets (spend carries), and
    scheduler counters. Incompatible state halts with the reason named.

    The post-restart leg continues research-only: the accepted revision is
    already promoted, and further revision needs a new maintained run. This
    narrowing is journaled; nothing about the scope changes silently.
    """
    from . import promote as promotemod

    target = Path(bundle_dir)
    bundle = report.read_bundle(target)
    manifest = bundle["manifest"]
    if manifest is None:
        raise ResumeError("no manifest: --expect-revision continues restart_required legs only")
    if manifest.get("status") != StopReason.RESTART_REQUIRED.value:
        raise ResumeError(f"run status is {manifest.get('status')}: --expect-revision "
                          "continues restart_required legs only")
    checkpoint = report.read_checkpoints(target)
    if checkpoint is None:
        raise ResumeError("no checkpoint committed: nothing to resume; start a new run")
    _require_checkpoint_keys(checkpoint)
    if checkpoint.get("restart_pending") != expect_revision:
        raise ResumeError(f"checkpoint waits for {checkpoint.get('restart_pending')}, "
                          f"asked {expect_revision or '(none)'}: refusing to continue")
    spec = RunSpec.from_dict(checkpoint["spec"])
    repo_root = checkpoint.get("repo_root")
    if not repo_root:
        raise ResumeError("restart leg needs the checkpointed repo_root")
    accepted = promotemod.Store(Path(repo_root) / "runs" / "promotions").latest_rev()
    if accepted != expect_revision:
        raise ResumeError(f"accepted revision is {accepted}, expected {expect_revision}: "
                          "refusing to continue on an unverified tree")
    code_now, csv_now = report.code_revision(), report.file_hash(spec.csv)
    if checkpoint["csv_hash"] != csv_now:
        raise ResumeError("inputs changed since the checkpoint: refusing unsafe resume")
    journal = _open_resume_journal(target, manifest["run_id"],
                                   checkpoint.get("journal_len", 0))
    journal.trusted_note("resume", "re-anchored",
                         f"code {checkpoint['code_hash'][:32]} -> {code_now[:32]} "
                         f"at {expect_revision}")
    journal.trusted_note("resume", "scope-narrowed",
                         f"post-restart leg continues research-only; revision already "
                         f"promoted as {expect_revision}")
    narrowed = dict(checkpoint.get("scope") or {})
    narrowed["maintenance"] = False
    narrowed.setdefault("repo_root", repo_root)
    narrowed.setdefault("check_cmd", checkpoint.get("check_cmd"))
    narrowed.setdefault("revise_rounds", spec.revise_rounds)
    resume_state = {"spec": checkpoint["spec"], "iterations": checkpoint["iterations"],
                    "pending": checkpoint["pending"], "seen": checkpoint["seen"],
                    "failed_q": checkpoint["failed_q"], "seq": checkpoint["seq"],
                    "budget": checkpoint["budget"],
                    "scheduler": checkpoint.get("scheduler"), "scope": narrowed,
                    "code_hash": code_now, "csv_hash": csv_now}
    result = run_cycle(spec.question, spec.csv, spec.target, spec.max_iterations,
                       spec.max_papers, muse, http, journal=journal,
                       maintenance=False, revise_rounds=spec.revise_rounds,
                       repo_root=repo_root, check_cmd=checkpoint.get("check_cmd"),
                       spec=spec, record_dir=str(target), resume=resume_state,
                       bandit=_bandit_from_checkpoint(checkpoint))
    return result, journal, spec, str(target)
