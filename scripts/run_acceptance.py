#!/usr/bin/env python3
"""Deterministic offline acceptance for the isolated researcher (Build 17).

Runs scripted end-to-end scenarios from a clean checkout with no provider
keys, no network, and no /tmp artefacts: review-only research, synthetic-CSV
analysis, a full research cycle, maintenance refusal on an uncontained host,
hermetic revision-logic tests, crash/resume, budget exhaustion, provider
restriction, the container boundary suite (when Docker exists), and the
export + secret-scan path.

Model replies and literature payloads are in-process doubles by necessity:
they cannot cross a process boundary. Anything requiring live providers or
a trusted revision path is NOT covered here and stays an explicit release
blocker (see the DECISION.md next to the generated bundle).

Usage:
    uv run --no-sync python scripts/run_acceptance.py [--out acceptance/<date>]
Exit 0 only when every scenario passes (explicit skips are allowed).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from canary import analysis as analysismod  # noqa: E402
from canary import cycle as cyclemod  # noqa: E402
from canary import data as datamod  # noqa: E402
from canary import modeling  # noqa: E402
from canary import rank as rankmod  # noqa: E402
from canary import report as reportmod  # noqa: E402
from canary import retrieval  # noqa: E402
from canary import synthesize  # noqa: E402
from canary.journal import Journal, emit  # noqa: E402
from canary.muse_client import KEY_VARS, MuseClient, RequestBlocked  # noqa: E402
from canary.redact import find_secrets, redact_text  # noqa: E402
from canary.revise import ContainmentBlocked  # noqa: E402
from canary.spec import RunBudget, RunSpec, StopReason  # noqa: E402

CONTAINER_CHECKS = 43


class Scripted:
    """Route canned replies by system-prompt kind. Strict: unknown calls fail."""

    model = "acceptance-scripted"

    def __init__(self, queues):
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls = []

    def complete(self, system, user, max_tokens=8000):
        if "research strategist" in system:
            kind = "follow"
        elif "precise research assistant" in system:
            kind = "review"
        elif "careful data scientist" in system:
            kind = "narrate"
        elif "reviewer" in system:
            kind = "assess"
        elif "unified diff" in system:
            kind = "diff"
        else:
            kind = "final"
        self.calls.append(kind)
        queue = self.queues.get(kind, [])
        if not queue:
            raise AssertionError(f"no scripted reply for {kind}")
        return queue.pop(0)


def grounded(prefix):
    return (f"{prefix} [1].\n```claims\n" + json.dumps([{
        "id": "c1", "text": "X was studied", "support": "supported",
        "evidence": [{"paper": 1, "span": "Study on X"}]}]) + "\n```")


def mock_http():
    def handler(req: httpx.Request) -> httpx.Response:
        if "openalex" in str(req.url):
            return httpx.Response(200, json={"results": [{
                "id": "W1", "title": "Study on X", "doi": "https://doi.org/10.1/x",
                "publication_year": 2023, "cited_by_count": 5,
                "authorships": [{"author": {"display_name": "A. Uthor"}}],
                "primary_location": {"source": {"display_name": "J X"}},
                "abstract_inverted_index": {"X": [0]}}]})
        return httpx.Response(200, json={"data": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


def scan_clean(root: Path) -> None:
    offenders = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix in (".pyc",):
            continue
        if path.stat().st_size > 5_000_000:
            continue
        if find_secrets(path.read_bytes()):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"secret-shaped text in: {offenders}"


def inspect_cli(bundle: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run([sys.executable, "-m", "canary.cli", "inspect", str(bundle)],
                          cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr[-500:]
    return json.loads(proc.stdout)


def scenario_review(out: Path) -> dict:
    spec = RunSpec(question="Does X matter?", max_papers=5, out_dir=str(out))
    run_id = reportmod.begin_run(out, spec, "review")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    emit(journal, "review", "start", spec.question)
    rspec = spec.research_spec()
    with mock_http() as http:
        papers, rep = retrieval.retrieve_with_report(rspec, http)
    assert papers, "mock literature returned nothing"
    ranked = rankmod.rerank(rspec.question, papers, len(papers))
    reportmod.record_retrieval(out, spec.question, ranked, rep)
    synth = synthesize.synthesize(rspec.question, ranked[:rspec.max_papers],
                                  Scripted({"review": [grounded("R")]}))
    supported = [c for c in synth.claims if c.support in ("supported", "partial")]
    assert synth.cited and supported, "review produced no grounded claims"
    reportmod.write_bundle(out, rspec, ranked[:rspec.max_papers], synth, journal,
                           rep["warning"])
    bundle = reportmod.read_bundle(out)
    assert bundle["manifest"]["status"] == "completed"
    assert inspect_cli(out)["status"] == "completed"
    scan_clean(out)
    return {"detail": f"cited={len(synth.cited)} grounded={len(supported)}",
            "artefacts": ["review.md", "provenance.json", "journal.jsonl"]}


def scenario_csv(out: Path) -> dict:
    csv_path = out / "input.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["f1", "f2", "t"])
        for i in range(80):
            writer.writerow([f"{0.01 * i:.2f}", f"{0.1 * (i % 7):.1f}", i % 2])
    question = "what predicts t?"
    spec = RunSpec(question=question, csv=str(csv_path), target="t", out_dir=str(out))
    run_id = reportmod.begin_run(out, spec, "analyze")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    df = datamod.load_csv(str(csv_path))
    seed = cyclemod.split_seed(str(csv_path), "t", question)
    prep = datamod.prepare(df, "t", seed=seed)
    res = modeling.run(prep)
    reportmod.record_analysis_inputs(out, question, str(csv_path), "t", prep, res)
    findings = analysismod.narrate(question, prep, res,
                                   Scripted({"narrate": ["f1 carries the signal."]}))
    reportmod.write_analysis_bundle(out, question, str(csv_path), "t", prep, res,
                                    findings, journal)
    assert prep.seed == cyclemod.split_seed(str(csv_path), "t", question)
    assert res.best and res.best_test is not None
    assert reportmod.read_bundle(out)["manifest"]["status"] == "completed"
    assert inspect_cli(out)["status"] == "completed"
    scan_clean(out)
    return {"detail": f"best={res.best} test={res.best_test} seed={prep.seed}",
            "artefacts": ["analysis.md", "analysis.json", "journal.jsonl"]}


def scenario_cycle(out: Path) -> dict:
    spec = RunSpec(question="seed?", max_iterations=3, max_papers=5, out_dir=str(out))
    run_id = reportmod.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    budget = RunBudget.from_spec(spec)
    muse = Scripted({
        "review": [grounded("R1"), grounded("R2")],
        "follow": ['[{"question": "q2?", "kind": "review", "rationale": "r"}]', "[]"],
        "final": ["Final."]})
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(),
                             journal=journal, spec=spec, budget=budget, record_dir=str(out))
    assert res.stopped == StopReason.CONVERGED, res.stopped
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run["stopped"] == "converged" and len(run["iterations"]) == 2
    assert run["usage"]["model_calls"] == 5 and not run["unanswered"]
    assert len(list((out / "checkpoints").glob("checkpoint-*.json"))) >= 2
    assert (out / "ops.jsonl").is_file()
    assert inspect_cli(out)["status"] == "converged"
    scan_clean(out)
    return {"detail": "2 iterations, converged, 5 calls",
            "artefacts": ["synthesis.md", "run.json", "journal.jsonl"]}


def scenario_maintenance_refusal(out: Path) -> dict:
    repo = out / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "code.py").write_text("X = 1\n", encoding="utf-8")
    spec = RunSpec(question="seed?", max_iterations=1, max_papers=5, maintenance=True,
                   out_dir=str(out))
    run_id = reportmod.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    try:
        cyclemod.run_cycle("seed?", None, None, 1, 5, Scripted({}), mock_http(),
                           journal=journal, maintenance=True, repo_root=str(repo),
                           spec=spec, budget=RunBudget.from_spec(spec), record_dir=str(out))
    except ContainmentBlocked as e:
        detail = str(e)[:160]
    else:
        raise AssertionError("maintenance run did not refuse on an uncontained host")
    assert (repo / "src" / "code.py").read_text() == "X = 1\n"
    assert not (repo / "runs").exists(), "refused run must not touch the tree"
    return {"detail": f"refused before any work: {detail}", "artefacts": ["journal.jsonl"]}


def scenario_maintenance_logic(out: Path) -> dict:
    files = ["tests/test_schedule.py", "tests/test_promotion.py",
             "tests/test_changeset.py", "tests/test_evalgate.py"]
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "not live", *files],
                          cwd=ROOT, capture_output=True, text=True, timeout=600)
    (out / "pytest.log").write_text(proc.stdout[-8000:] + proc.stderr[-2000:],
                                    encoding="utf-8")
    tail = [ln for ln in proc.stdout.splitlines() if "passed" in ln or "failed" in ln]
    assert proc.returncode == 0, f"revision-logic suite failed:\n{proc.stdout[-2000:]}"
    return {"detail": tail[-1].strip() if tail else "suite green",
            "artefacts": ["pytest.log"]}


def scenario_crash_resume(out: Path) -> dict:
    from canary.spec import Cancelled

    class Crashing(Scripted):
        def __init__(self, queues):
            super().__init__(queues)
            self.n = 0

        def complete(self, system, user, max_tokens=8000):
            self.n += 1
            if self.n == 3:
                raise Cancelled("simulated crash")
            return Scripted.complete(self, system, user, max_tokens)

    spec = RunSpec(question="seed?", max_iterations=3, max_papers=5, out_dir=str(out))
    run_id = reportmod.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    broken = Crashing({
        "review": [grounded("R1"), grounded("R2")],
        "follow": ['[{"question": "q2?", "kind": "review", "rationale": "r"}]'],
        "final": ["Final."]})
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, broken, mock_http(),
                             journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                             record_dir=str(out))
    assert res.stopped == StopReason.CANCELLED and len(res.iterations) == 1
    assert reportmod.read_bundle(out)["status"] == "interrupted"
    cont = Scripted({"review": [grounded("R2")], "follow": ["[]"], "final": ["Final."]})
    resumed, journal2, _, _ = cyclemod.resume_cycle(out, cont, mock_http())
    assert [i.question for i in resumed.iterations] == ["seed?", "q2?"]
    assert resumed.stopped == StopReason.CONVERGED
    assert resumed.usage["model_calls"] == 5
    reportmod.write_cycle_bundle(out, "seed?", resumed, journal2, spec)
    assert inspect_cli(out)["status"] == "converged"
    scan_clean(out)
    return {"detail": "crash at call 3, resumed to converged, 5 calls carried",
            "artefacts": ["synthesis.md", "run.json", "journal.jsonl"]}


def scenario_budget(out: Path) -> dict:
    spec = RunSpec(question="seed?", max_iterations=5, max_papers=5, max_model_calls=2,
                   out_dir=str(out))
    run_id = reportmod.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    muse = Scripted({
        "review": [grounded("R1"), grounded("R2")],
        "follow": ['[{"question": "q2?", "kind": "review", "rationale": "r"}]'],
        "final": ["Final."]})
    res = cyclemod.run_cycle("seed?", None, None, 5, 5, muse, mock_http(),
                             journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                             record_dir=str(out))
    assert res.stopped == StopReason.BUDGET_EXHAUSTED, res.stopped
    assert res.unanswered == ("q2?",) and len(res.iterations) == 1
    assert res.usage == {"model_calls": 2, "tokens": 0, "tokens_reported": False}
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    assert (out / "iterations" / "iter1-review.md").is_file()
    assert inspect_cli(out)["status"] == "budget_exhausted"
    scan_clean(out)
    return {"detail": "stopped honestly at 2/2 calls, q2? carried forward",
            "artefacts": ["synthesis.md", "run.json", "journal.jsonl"]}


def scenario_provider(out: Path) -> dict:
    class Blocked(Scripted):
        def complete(self, system, user, max_tokens=8000):
            raise RequestBlocked("broker refused: no allowance")

    spec = RunSpec(question="seed?", max_iterations=3, max_papers=5, out_dir=str(out))
    run_id = reportmod.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    try:
        cyclemod.run_cycle("seed?", None, None, 3, 5, Blocked({}), mock_http(),
                           journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                           record_dir=str(out))
    except RequestBlocked:
        pass  # blocked before the first completion: the run raises, honestly
    else:
        raise AssertionError("provider block did not surface")
    assert (out / "checkpoints" / "checkpoint-0000.json").is_file()
    assert (out / "journal.jsonl").is_file()  # seq-0 state stands for inspection
    saved = os.environ.pop("CANARY_GATE_TOKEN", None)
    try:
        try:
            MuseClient()
        except RequestBlocked as e:
            gate_detail = str(e)[:120]
        else:
            raise AssertionError("client without a broker token did not refuse")
    finally:
        if saved is not None:
            os.environ["CANARY_GATE_TOKEN"] = saved
    key_presence = {var: bool(os.environ.get(var)) for var in (*KEY_VARS, "CANARY_GATE_TOKEN")}
    (out / "key_presence.json").write_text(json.dumps(key_presence, indent=2) + "\n")
    scan_clean(out)
    return {"detail": f"run blocked honestly; keyless client refuses ({gate_detail})",
            "artefacts": ["journal.jsonl", "key_presence.json", "checkpoints/checkpoint-0000.json"]}


def scenario_containment(out: Path) -> dict:
    if shutil.which("docker") is None:
        return {"status": "skip", "detail": "no docker on PATH",
                "artefacts": []}
    info = subprocess.run(["docker", "info"], capture_output=True, timeout=60)
    if info.returncode != 0:
        return {"status": "skip", "detail": "docker daemon unreachable",
                "artefacts": []}
    proc = subprocess.run(["bash", "scripts/container_verify.sh"], cwd=ROOT,
                          capture_output=True, text=True, timeout=600)
    (out / "container_verify.log").write_text(proc.stdout[-12000:] + proc.stderr[-2000:],
                                              encoding="utf-8")
    count = proc.stdout.count(": true")
    assert proc.returncode == 0 and count == CONTAINER_CHECKS, \
        f"boundary suite: rc={proc.returncode} true={count}/{CONTAINER_CHECKS}"
    leftovers = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                               capture_output=True, text=True, timeout=60).stdout
    assert "canary-" not in leftovers, f"disposable leftovers: {leftovers}"
    return {"detail": f"{count}/{CONTAINER_CHECKS} boundary checks, no leftovers",
            "artefacts": ["container_verify.log"]}


def scenario_export(out: Path, bundle_src: Path) -> dict:
    dest = out / "exported"
    tar = subprocess.run(["tar", "-cf", "-", "-C", str(bundle_src), "."],
                         capture_output=True, timeout=120)
    assert tar.returncode == 0, tar.stderr.decode()[-300:]
    exp = subprocess.run([sys.executable, "scripts/export_bundle.py", str(dest)],
                         input=tar.stdout, cwd=ROOT, capture_output=True, timeout=120)
    assert exp.returncode == 0, exp.stderr.decode()[-500:]
    scan = subprocess.run([sys.executable, "scripts/scan_export.py", str(dest)],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    (out / "scan.log").write_text(scan.stdout + scan.stderr, encoding="utf-8")
    assert scan.returncode == 0, f"export scan failed:\n{scan.stdout[-1000:]}"
    manifest = json.loads((dest / "manifest.canary.json").read_text(encoding="utf-8"))
    assert manifest["count"] > 0 and manifest["files"]
    return {"Detail": f"{manifest['count']} files exported, manifest valid, scan clean",
            "artefacts": ["exported/manifest.canary.json", "scan.log"]}


SCENARIOS = [
    ("review-only", scenario_review),
    ("synthetic-csv", scenario_csv),
    ("cycle-research", scenario_cycle),
    ("maintenance-refusal", scenario_maintenance_refusal),
    ("maintenance-logic", scenario_maintenance_logic),
    ("crash-resume", scenario_crash_resume),
    ("budget-exhaustion", scenario_budget),
    ("provider-restriction", scenario_provider),
    ("containment", scenario_containment),
]


def repo_rev() -> str:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)
        return proc.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic offline acceptance.")
    ap.add_argument("--out", default=None, help="output dir (default acceptance/<date>)")
    args = ap.parse_args(argv)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = Path(args.out) if args.out else ROOT / "acceptance" / day
    out.mkdir(parents=True, exist_ok=True)
    verdicts = []
    failed = False
    for name, fn in SCENARIOS:
        start = time.monotonic()
        sdir = out / "scenarios" / name
        sdir.mkdir(parents=True, exist_ok=True)
        try:
            result = fn(sdir)
            status = result.pop("status", "pass")
            verdicts.append({"scenario": name, "status": status,
                             "detail": result.get("detail", result.get("Detail", "")),
                             "duration_s": round(time.monotonic() - start, 1),
                             "artefacts": [f"scenarios/{name}/{a}"
                                           for a in result.get("artefacts", [])]})
            print(f"[{status}] {name}: {verdicts[-1]['detail']}", flush=True)
        except Exception as e:  # noqa: BLE001 - acceptance records, then fails loud
            failed = True
            detail = redact_text(f"{type(e).__name__}: {e}")[:400]
            (sdir / "FAILURE.txt").write_text(traceback.format_exc(limit=8), encoding="utf-8")
            verdicts.append({"scenario": name, "status": "fail", "detail": detail,
                             "duration_s": round(time.monotonic() - start, 1),
                             "artefacts": [f"scenarios/{name}/FAILURE.txt"]})
            print(f"[fail] {name}: {detail}", flush=True)
    # Export runs against the cycle bundle once research scenarios complete.
    if not failed:
        start = time.monotonic()
        sdir = out / "scenarios" / "export-scan"
        sdir.mkdir(parents=True, exist_ok=True)
        try:
            result = scenario_export(sdir, out / "scenarios" / "cycle-research")
            verdicts.append({"scenario": "export-scan", "status": "pass",
                             "detail": result["Detail"],
                             "duration_s": round(time.monotonic() - start, 1),
                             "artefacts": [f"scenarios/export-scan/{a}"
                                           for a in result["artefacts"]]})
            print(f"[pass] export-scan: {result['Detail']}", flush=True)
        except Exception as e:  # noqa: BLE001
            failed = True
            detail = redact_text(f"{type(e).__name__}: {e}")[:400]
            verdicts.append({"scenario": "export-scan", "status": "fail", "detail": detail,
                             "duration_s": round(time.monotonic() - start, 1),
                             "artefacts": []})
            print(f"[fail] export-scan: {detail}", flush=True)
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "repo_rev": repo_rev(), "python": sys.version.split()[0],
                "verdicts": verdicts,
                "counts": {s: sum(1 for v in verdicts if v["status"] == s)
                           for s in ("pass", "fail", "skip")}}
    (out / "verdicts.jsonl").write_text(
        "\n".join(json.dumps(v) for v in verdicts) + "\n", encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Acceptance {day}", "",
             f"Revision: `{manifest['repo_rev']}` | Python {manifest['python']}", "",
             "| scenario | status | detail |", "|---|---|---|"]
    for v in verdicts:
        lines.append(f"| {v['scenario']} | {v['status']} | {v['detail']} |")
    lines += ["",
              "Reproduce: `uv sync --locked && uv run --no-sync python "
              "scripts/run_acceptance.py`",
              "Live providers and trusted revision are NOT covered here; see DECISION.md."]
    (out / "ACCEPTANCE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"verdicts: {manifest['counts']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
