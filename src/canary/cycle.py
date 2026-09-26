"""Cycle: answers become next questions until budget or convergence."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

from . import analysis, data as datamod, modeling, rank, report, retrieval, synthesize
from .journal import Journal, emit
from .muse_client import RequestBlocked
from .spec import ResearchSpec
from .synthesize import Completer

MAX_ITERATIONS = 5
FOLLOWUP_SYSTEM = (
    "You are a research strategist. Given the findings so far, propose follow-up "
    "research questions that are answerable and non-redundant. Reply with ONLY a "
    "JSON array of objects with keys: question (string), kind (\"review\" or "
    "\"analyze\"), rationale (one sentence). Prefer \"analyze\" only when the "
    "available dataset plausibly contains the needed variables; otherwise use "
    "\"review\". If nothing worthwhile remains, reply with []. At most 3 items."
)

SYNTHESIS_SYSTEM = (
    "You are a senior researcher. Synthesize the iteration findings below into a "
    "final report: headline answer, supporting evidence per iteration, limits, "
    "and the single most important next question. Under 400 words. Never invent "
    "numbers; cite iterations as (iter N)."
)


@dataclass(frozen=True)
class Followup:
    question: str
    kind: str  # "review" | "analyze"
    rationale: str


@dataclass
class Iteration:
    n: int
    question: str
    kind: str
    summary: str  # short text for next-round context
    detail: str  # full markdown written to disk
    provenance: dict = field(default_factory=dict)


def parse_followups(text: str) -> list[Followup]:
    """Parse Muse's JSON; anything unparseable means stop (never crash)."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[Followup] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        q = str(item.get("question", "")).strip()
        kind = str(item.get("kind", "")).strip().lower()
        if not q or kind not in ("review", "analyze"):
            continue
        out.append(Followup(question=q[:500], kind=kind, rationale=str(item.get("rationale", ""))[:300]))
    return out[:3]


def propose(history: str, dataset_hint: str, client: Completer) -> list[Followup]:
    user = f"Dataset available: {dataset_hint}\n\nFindings so far:\n{history}\n\nPropose follow-ups."
    return parse_followups(client.complete(FOLLOWUP_SYSTEM, user))


def run_review(question: str, max_papers: int, http: httpx.Client, muse: Completer, journal: Journal | None = None) -> Iteration:
    emit(journal, "review", "start", question[:200])
    spec = ResearchSpec(question=question, max_papers=max_papers)
    papers = retrieval.retrieve(spec, http)
    emit(journal, "review", "retrieved", f"{len(papers)} candidates")
    if not papers:
        raise RuntimeError("no papers found for this question")
    top = rank.rerank(spec.question, papers, spec.max_papers)
    synth = synthesize.synthesize(spec.question, top, muse)
    emit(journal, "review", "synthesized", f"{len(top)} papers, cited {len(synth.cited)}")
    detail = report.render_markdown(spec, top, synth)
    cited = ", ".join(f"[{i}]" for i in synth.cited[:6]) or "none"
    summary = f"Q: {question}\nReview of {len(top)} papers (cited {cited}). {synth.text[:800]}"
    prov = {"kind": "review", "papers": [p.title for p in top], "model": synth.model}
    return Iteration(n=0, question=question, kind="review", summary=summary, detail=detail, provenance=prov)


def run_analyze(question: str, csv: str, target: str, muse: Completer, journal: Journal | None = None) -> Iteration:
    emit(journal, "analyze", "start", f"{csv} target={target}")
    df = datamod.load_csv(csv)
    prep = datamod.prepare(df, target)
    emit(journal, "analyze", "prepared", f"{prep.profile.n_rows} rows, {prep.profile.task}")
    res = modeling.run(prep)
    emit(journal, "analyze", "modeled", f"{res.best} test={res.best_test} baseline={res.baseline_test}")
    findings = analysis.narrate(question, prep, res, muse)
    emit(journal, "analyze", "narrated", f"warnings={len(res.warnings)}")
    detail = report.render_analysis(question, csv, target, prep, res, findings)
    summary = f"Q: {question}\n{res.task} on {prep.profile.n_rows} rows: {res.best} test {res.best_test} vs baseline {res.baseline_test}. {findings.text[:600]}"
    prov = {
        "kind": "analyze",
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
    stopped: str  # "converged" | "max_iterations" | "failed: ..."
    unanswered: tuple[str, ...] = ()  # proposed but never executed


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
) -> CycleResult:
    if not 1 <= max_iterations <= MAX_ITERATIONS:
        raise ValueError(f"max_iterations must be 1..{MAX_ITERATIONS}")
    if bool(csv) != bool(target):
        raise ValueError("--csv and --target must be given together")
    own = http is None
    http = http or httpx.Client(headers={"User-Agent": "Canary/0.1"})
    iterations: list[Iteration] = []
    stopped = "converged"
    failed_q: str | None = None
    revising = maintenance and repo_root is not None
    if maintenance and repo_root is None:
        emit(journal, "cycle", "revise-disabled", "maintenance needs repo_root")
    emit(journal, "cycle", "start", f"seed={question[:150]} max_iter={max_iterations}")
    try:
        pending: list[Followup] = [Followup(question=question, kind="review", rationale="seed")]
        if csv and target:
            pending.append(Followup(question=f"what predicts {target}?", kind="analyze", rationale="seed"))
        seen = {normalize(j.question) for j in pending}
        while len(iterations) < max_iterations:
            if not pending:  # propose only once queued work drains
                history = "\n\n---\n\n".join(f"[iter {i.n}] {i.summary}" for i in iterations)
                hint = f"{csv} (target {target})" if csv else "none — reviews only"
                try:
                    followups = propose(history, hint, muse)
                except RequestBlocked:
                    raise
                except Exception as e:
                    followups = []
                    emit(journal, "cycle", "propose-failed", f"{type(e).__name__}: {str(e)[:200]}")
                if not csv:  # no dataset: only reviews are executable
                    followups = [f for f in followups if f.kind == "review"]
                fresh: list[Followup] = []
                for f in followups:
                    key = normalize(f.question)
                    if key not in seen:
                        seen.add(key)
                        fresh.append(f)
                if not fresh:  # dry round: nothing new proposed
                    stopped = "converged"
                    break
                pending.extend(fresh)
            job = pending.pop(0)
            try:
                if job.kind == "analyze" and csv and target:
                    it = run_analyze(job.question, csv, target, muse, journal)
                else:
                    it = run_review(job.question, max_papers, http, muse, journal)
            except RequestBlocked:
                raise
            except Exception as e:
                stopped = f"failed: {e}"
                failed_q = job.question
                emit(journal, "cycle", "failed", str(e)[:300])
                break
            it.n = len(iterations) + 1
            iterations.append(it)
            emit(journal, "cycle", "iter-done", f"n={it.n} kind={it.kind}")
            if revising:  # iterative maintenance as it goes, not only at the end
                _mid_run_revise(journal, it, repo_root, muse, check_cmd)
            if len(iterations) >= max_iterations:
                stopped = "max_iterations"
        if not iterations:
            raise RuntimeError(stopped)
        unanswered = tuple(([failed_q] if failed_q else []) + [j.question for j in pending])
        history = "\n\n---\n\n".join(f"[iter {i.n}] {i.summary}" for i in iterations)
        tail = f"\n\nUnanswered (budget ran out, carry forward): {list(unanswered)}" if unanswered else ""
        synthesis = muse.complete(SYNTHESIS_SYSTEM, f"Iterations:\n{history}{tail}")
        if not synthesis.strip():
            raise RuntimeError("Muse API returned an empty final synthesis")
        emit(journal, "cycle", "done", f"iters={len(iterations)} stopped={stopped}")
        if revising:
            try:
                from .revise import revise_from_journal

                outcome = f"{len(iterations)} iterations, stopped={stopped}"
                doc, rep = revise_from_journal(
                    journal.text() if journal else "", outcome, repo_root, muse,
                    rounds=revise_rounds, check_cmd=check_cmd, journal=journal,
                )
                emit(journal, "cycle", "revised", f"kept={rep.kept} reverted={rep.reverted} skipped={rep.skipped}")
                if outbox is not None:
                    outbox["revision"] = (doc, rep)
            except RequestBlocked:
                raise
            except Exception as e:
                emit(journal, "cycle", "revise-failed", str(e)[:200])
        return CycleResult(tuple(iterations), synthesis, muse.model, stopped, unanswered)
    finally:
        if own:
            http.close()


def _mid_run_revise(journal: Journal | None, it: Iteration, repo_root: str | None, muse: Completer, check_cmd: list[str] | None) -> None:
    """One bounded revise pass on the latest iteration's notes. Never raises.

    Note: kept patches land on disk and are validated, but this process keeps
    running the already-imported modules. Revisions activate on the next run.
    """
    if journal is None or repo_root is None:
        return
    try:
        from .revise import revise_from_journal

        _, rep = revise_from_journal(
            journal.text(), f"mid-run after iter {it.n} ({it.kind})", repo_root, muse,
            rounds=1, check_cmd=check_cmd, journal=journal,
        )
        emit(journal, "cycle", "mid-revised", f"iter={it.n} kept={rep.kept}")
    except RequestBlocked:
        raise
    except Exception as e:
        emit(journal, "cycle", "mid-revise-failed", str(e)[:200])
