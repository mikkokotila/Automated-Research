"""Isolated worker: run one benchmark task against a candidate tree.

Reads {"task": ..., "input": ...} on stdin, prints {"ok": ..., "output": ...}
on stdout. The worker NEVER sees expected outputs; the controller scores.
Anything but a clean schema-valid result is a failure the controller records.
"""

from __future__ import annotations

import json
import sys
import time


def _task_relevance(payload: dict) -> dict:
    from canary import rank
    from canary.papers import Paper

    papers = [Paper(ref=p["ref"], title=p["title"], abstract=p.get("abstract", ""),
                    citations=p.get("citations", 0)) for p in payload["papers"]]
    coverage = rank.question_coverage(payload["question"], papers)
    return {"verdict": coverage["verdict"], "best_cover": coverage["best_cover"],
            "min_cover": coverage["min_cover"]}


def _task_claims(payload: dict) -> dict:
    from canary import synthesize
    from canary.papers import Paper

    papers = [Paper(ref=p["ref"], title=p["title"], abstract=p.get("abstract", ""),
                    evidence=p.get("evidence", "abstract")) for p in payload["papers"]]
    claims, validation = synthesize.validate_claims(payload["claims"], papers)
    return {"supports": {c.id: c.support for c in claims},
            "rejected": validation["rejected"]}


def _task_abstain(payload: dict) -> dict:
    import httpx

    from canary import cycle as cyclemod

    def handler(req: httpx.Request) -> httpx.Response:
        if "openalex" in str(req.url):
            return httpx.Response(200, json=payload["openalex"])
        return httpx.Response(200, json={"data": []})

    class Muse:
        model = "scripted"

        def complete(self, system, user, max_tokens=8000):
            return payload["synth_text"]

    http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        cyclemod.run_review(payload["question"], 5, http, Muse())
        return {"outcome": "answered"}
    except cyclemod.NoEvidence:
        return {"outcome": "NoEvidence"}
    finally:
        http.close()


def _task_leakage(payload: dict) -> dict:
    import numpy as np
    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.datasets import make_classification

    from canary import data as datamod, modeling

    records: list[frozenset] = []

    class Recording(ColumnTransformer):
        def fit_transform(self, X, y=None, **params):
            idx = X.index if hasattr(X, "index") else pd.DataFrame(X).index
            records.append(frozenset(idx))
            return super().fit_transform(X, y, **params)

    def factory(numeric, categorical):
        base = datamod.build_preprocessor(numeric, categorical)
        return Recording(base.transformers)

    seed = int(payload.get("seed", 0))
    X, y = make_classification(n_samples=120, n_features=5, n_informative=3,
                               random_state=seed)
    df = pd.DataFrame(X, columns=[f"f{i}" for i in range(5)])
    df["t"] = y
    prep = datamod.prepare(df, "t")
    modeling.run(prep, preprocessor_factory=factory)
    train_idx = frozenset(prep.train_frame.index)
    return {"n_fits": len(records),
            "all_strict_subsets": bool(records) and all(r < train_idx for r in records)}


def _task_resume(payload: dict) -> dict:
    import tempfile
    from pathlib import Path

    import httpx

    from canary import cycle as cyclemod, report
    from canary.journal import Journal
    from canary.spec import RunBudget, RunSpec

    class Scripted:
        model = "scripted"

        def __init__(self, queues):
            self.queues = queues

        def complete(self, system, user, max_tokens=8000):
            if "research strategist" in system:
                kind = "follow"
            elif "precise research assistant" in system:
                kind = "review"
            else:
                kind = "final"
            return self.queues[kind].pop(0)

    class Crash(Scripted):
        def __init__(self, queues, crash_on):
            super().__init__(queues)
            self._n = 0
            self._crash_on = crash_on

        def complete(self, system, user, max_tokens=8000):
            self._n += 1
            if self._n == self._crash_on:
                raise KeyboardInterrupt("simulated crash")
            return super().complete(system, user, max_tokens)

    def mock_http() -> httpx.Client:
        def handler(req: httpx.Request) -> httpx.Response:
            if "openalex" in str(req.url):
                return httpx.Response(200, json={"results": [{
                    "id": "W1", "title": "Study on X",
                    "doi": "https://doi.org/10.1/x", "publication_year": 2023,
                    "cited_by_count": 5,
                    "authorships": [{"author": {"display_name": "A"}}],
                    "primary_location": {"source": {"display_name": "J"}},
                    "abstract_inverted_index": {"X": [0]}}]})
            return httpx.Response(200, json={"data": []})

        return httpx.Client(transport=httpx.MockTransport(handler))

    def fresh(out, muse):
        spec = RunSpec(question="seed?", max_iterations=3, max_papers=5)
        run_id = report.begin_run(out, spec, "cycle")
        journal = Journal(out / "journal.jsonl", run_id=run_id)
        budget = RunBudget.from_spec(spec)
        return cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(),
                                  journal=journal, spec=spec, budget=budget,
                                  record_dir=str(out))

    def grounded(prefix):
        # Anchored supported claim: under the Build 16 grounding contract,
        # marker-only output truthfully stops no_progress; this fixture tests
        # crash/resume equivalence under convergence, so it grounds its output.
        return (f"{prefix} [1].\n```claims\n" + json.dumps([{
            "id": "c1", "text": "X was studied", "support": "supported",
            "evidence": [{"paper": 1, "span": "Study on X"}]}]) + "\n```")

    crash_on = int(payload.get("crash_on", 3))
    follow_first = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]']
    tmp = Path(tempfile.mkdtemp(prefix="eval-resume-"))
    whole = fresh(tmp / "whole", Scripted({
        "review": [grounded("R1"), grounded("R2")],
        "follow": follow_first + ["[]"],
        "final": ["Final."]}))
    fresh(tmp / "broken", Crash(
        {"review": [grounded("R1"), grounded("R2")], "follow": list(follow_first),
         "final": ["Final."]}, crash_on=crash_on))
    # Crash inside propose (call 2) leaves pending empty: the resume must
    # re-propose; a later crash leaves queued work to continue instead.
    resume_follow = follow_first + ["[]"] if crash_on == 2 else ["[]"]
    resumed, _, _, _ = cyclemod.resume_cycle(tmp / "broken", Scripted({
        "review": [grounded("R2")], "follow": resume_follow, "final": ["Final."]}),
        mock_http())
    same = ([i.question for i in resumed.iterations] ==
            [i.question for i in whole.iterations] and
            resumed.synthesis == whole.synthesis and resumed.stopped == whole.stopped)
    return {"equivalent": same, "stopped": resumed.stopped.value}


def _task_budget(payload: dict) -> dict:
    from canary.spec import BudgetExhausted, RunBudget

    budget = RunBudget(int(payload.get("max_calls", 2)), 10_000_000, 600)
    raised = False
    try:
        for _ in range(int(payload.get("max_calls", 2)) + 1):
            budget.reserve_call()
    except BudgetExhausted:
        raised = True
    return {"exhaustion_raised": raised, "calls": budget.calls}


def _task_sleep(payload: dict) -> dict:
    """Deterministic slow task: the timeout test's fixture, nothing else."""
    try:
        seconds = float(payload.get("seconds", 30))
    except (TypeError, ValueError):
        seconds = 30.0
    seconds = min(max(seconds, 0.0), 120.0)
    time.sleep(seconds)
    return {"slept": seconds}


TASKS = {
    "relevance": _task_relevance,
    "claims": _task_claims,
    "abstain": _task_abstain,
    "leakage": _task_leakage,
    "resume": _task_resume,
    "budget": _task_budget,
    "sleep": _task_sleep,
}


def main() -> int:
    try:
        request = json.load(sys.stdin)
        fn = TASKS[request["task"]]
    except (ValueError, KeyError):
        print(json.dumps({"ok": False, "error": "unknown task"}))
        return 0
    try:
        output = fn(request.get("input", {}))
    except Exception as e:  # worker failures are data, never tracebacks-as-proof
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}))
        return 0
    print(json.dumps({"ok": True, "output": output}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
