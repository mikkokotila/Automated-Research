"""Trusted controller: build trees, run workers, score outputs, decide.

The controller owns task definitions, limits, expectations, and results.
Candidate code runs only inside disposable worker trees and its outputs are
scored as data. The controller never imports candidate code: every comparison
here is a primitive equality check on parsed JSON.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PACK_DIR = Path(__file__).resolve().parent


def _thresholds() -> dict:
    return json.loads((PACK_DIR / "thresholds.json").read_text(encoding="utf-8"))


_THRESHOLDS = _thresholds()
PACK_VERSION = _THRESHOLDS["pack_version"]
TASK_TIMEOUT_S = _THRESHOLDS["task_timeout_s"]
STDOUT_CAP = _THRESHOLDS["stdout_cap_bytes"]


class GateError(Exception):
    """Controller-side failure. Fails closed: no decision without evidence."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def thresholds_hash() -> str:
    """Hash of the frozen decision inputs: versions, sets, and limits."""
    payload = json.dumps({"pack_version": PACK_VERSION, "timeout_s": TASK_TIMEOUT_S,
                          "dev": _fixture_ids(PACK_DIR / "dev.json"),
                          "acceptance": _fixture_ids(PACK_DIR / "acceptance.json")},
                         sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _fixture_ids(path: Path) -> list[str]:
    return [f["id"] for f in json.loads(path.read_text(encoding="utf-8"))["fixtures"]]


def load_set(name: str, log_path: str | Path | None = None, reason: str = "") -> list[dict]:
    """Load a fixture set. Acceptance reads are access-logged when log_path set."""
    if name == "acceptance" and log_path is not None:
        with Path(log_path).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": _utcnow(), "set": name,
                                "pack_version": PACK_VERSION, "reason": reason}) + "\n")
    path = PACK_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))["fixtures"]


def build_tree(repo: str | Path, base_rev: str, diff: str | None,
               dest: str | Path | None = None) -> Path:
    """Disposable worker tree: pristine archive of base_rev plus candidate diff.

    The diff is policy-checked before it touches the tree: anything outside
    src/canary/ (including this gate) rejects the candidate outright.
    """
    from canary.changeset import check_policy, parse_unified_diff
    from canary.revise import target_allowed

    repo = Path(repo)
    if diff:
        check_policy(parse_unified_diff(diff), target_allowed)
    work = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="eval-tree-"))
    work.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "archive", base_rev], cwd=repo, capture_output=True,
                             timeout=120)
    if archive.returncode != 0:
        raise GateError(f"cannot archive {base_rev}: {archive.stderr.decode()[:200]}")
    extract = subprocess.run(["tar", "-x", "-C", str(work)], input=archive.stdout,
                             capture_output=True, timeout=120)
    if extract.returncode != 0:
        raise GateError(f"cannot extract {base_rev}: {extract.stderr.decode()[:200]}")
    if diff:
        applied = subprocess.run(["git", "apply", "-"], input=diff.encode(), cwd=work,
                                 capture_output=True, timeout=120)
        if applied.returncode != 0:
            raise GateError(f"candidate does not apply to {base_rev}: "
                            f"{applied.stderr.decode()[:300]}")
    # The worker implementation is controller-owned: it always comes from this
    # gate, never from the candidate tree (whose evalpack/ may be absent or
    # stale; the policy already forbids candidates from touching it). Only the
    # worker ships — fixture expectations never enter the worker tree, so a
    # candidate cannot read them to forge matching outputs.
    shutil.rmtree(work / "evalpack", ignore_errors=True)
    (work / "evalpack").mkdir()
    shutil.copy(PACK_DIR / "__init__.py", work / "evalpack" / "__init__.py")
    shutil.copy(PACK_DIR / "worker.py", work / "evalpack" / "worker.py")
    return work


def worker_env(tree: str | Path) -> dict:
    """Scrubbed worker environment: deterministic, secret-free, minimal."""
    tree = Path(tree)
    return {"PATH": "/usr/bin:/bin:/usr/local/bin",
            "PYTHONPATH": str(tree / "src") + ":" + str(tree),
            "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": tempfile.gettempdir()}


def run_task(tree: str | Path, task: str, task_input: dict,
             timeout_s: int = TASK_TIMEOUT_S) -> dict:
    """Run one task in a scrubbed subprocess. Returns scored-or-failed detail."""
    tree = Path(tree)
    request = json.dumps({"task": task, "input": task_input})
    env = worker_env(tree)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "evalpack.worker"], input=request.encode(), cwd=tree,
            capture_output=True, timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout_s}s"}
    if len(proc.stdout) > STDOUT_CAP:
        return {"ok": False, "error": "stdout exceeded cap"}
    if proc.returncode != 0:
        return {"ok": False, "error": f"worker exited {proc.returncode}: "
                                      f"{proc.stderr.decode()[:300]}"}
    try:
        envelope = json.loads(proc.stdout.decode())
    except ValueError:
        return {"ok": False, "error": "worker stdout is not JSON"}
    if not isinstance(envelope, dict) or "ok" not in envelope:
        return {"ok": False, "error": "worker envelope failed schema"}
    return envelope


def run_pack(tree: str | Path, fixtures: list[dict]) -> dict[str, dict]:
    """Run every fixture; score each output against its frozen expectation."""
    results: dict[str, dict] = {}
    for fixture in fixtures:
        detail = run_task(tree, fixture["task"], fixture["input"])
        if not detail.get("ok"):
            results[fixture["id"]] = {"pass": False, "error": detail.get("error"),
                                      "output": None}
            continue
        output = detail.get("output")
        match = output == fixture["expected"]
        results[fixture["id"]] = {"pass": match, "output": output,
                                  "expected": fixture["expected"],
                                  "error": None if match else "output mismatch"}
    return results


def decide(base: dict[str, dict], candidate: dict[str, dict],
           expected_fixes: list[str]) -> dict:
    """Accept iff nothing regressed and every predeclared fix resolved.

    expected_fixes must be fixed BEFORE the candidate runs; they name fixtures
    failing at baseline that the candidate claims to repair. Silence (no
    predeclared benefit) or noise (unclaimed newly-passing fixtures) never
    counts as improvement.
    """
    pass_to_pass = sorted(fid for fid, r in base.items() if r["pass"])
    regressions = sorted(fid for fid in pass_to_pass if not candidate.get(fid, {}).get("pass"))
    unknown = sorted(fid for fid in expected_fixes if fid not in base)
    not_failing = sorted(fid for fid in expected_fixes
                         if fid in base and base[fid]["pass"])
    unresolved = sorted(fid for fid in expected_fixes
                        if fid in base and not candidate.get(fid, {}).get("pass"))
    verdict = "accept" if (not regressions and not unknown and not not_failing
                           and not unresolved and expected_fixes) else "reject"
    reasons = []
    if not expected_fixes:
        reasons.append("no predeclared benefit: nothing claimed, nothing accepted")
    reasons += [f"regression: {fid}" for fid in regressions]
    reasons += [f"unknown fixture: {fid}" for fid in unknown]
    reasons += [f"not failing at baseline: {fid}" for fid in not_failing]
    reasons += [f"unresolved: {fid}" for fid in unresolved]
    return {"verdict": verdict, "reasons": reasons, "pass_to_pass": pass_to_pass,
            "regressions": regressions, "expected_fixes": list(expected_fixes),
            "unresolved": unresolved}


def render_report(decision: dict, base: dict[str, dict], candidate: dict[str, dict],
                  base_rev: str, candidate_desc: str) -> str:
    lines = [
        "# Evaluation gate decision",
        "",
        f"- verdict: **{decision['verdict']}**",
        f"- pack: {PACK_VERSION} (thresholds {thresholds_hash()[:24]})",
        f"- base: {base_rev}",
        f"- candidate: {candidate_desc}",
        f"- at: {_utcnow()}",
        "",
        "## Reasons",
        "",
    ]
    lines += [f"- {r}" for r in decision["reasons"]] or ["- none"]
    lines += ["", "| fixture | base | candidate |", "|---|---|---|"]
    for fid in sorted(set(base) | set(candidate)):
        b = "pass" if base.get(fid, {}).get("pass") else "FAIL"
        c = "pass" if candidate.get(fid, {}).get("pass") else "FAIL"
        lines.append(f"| {fid} | {b} | {c} |")
    lines += ["",
              "_Deterministic fixtures only. A pass is not proof of scientific "
              "truth, and green output says nothing about containment._",
              ""]
    return "\n".join(lines)


def teardown(tree: str | Path) -> None:
    shutil.rmtree(tree, ignore_errors=True)
