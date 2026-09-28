"""Run records: parse an exported bundle into one dashboard row.

Defensive by design: bundles vary by kind (review/analyze/cycle), age, and
outcome. Missing files yield empty lists and "unknown" fields, never an
exception — the dashboard must show a degraded row, not crash the render.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _journal_events(bundle: Path) -> list[dict]:
    events = []
    try:
        lines = (bundle / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def _iterations(bundle: Path, finished: bool) -> list[dict]:
    iters = []
    for path in sorted(glob.glob(str(bundle / "iterations" / "iter*.json"))):
        d = _load_json(Path(path), {})
        if not isinstance(d, dict):
            continue
        iters.append({"n": d.get("n"), "kind": d.get("kind", "?"),
                      "question": d.get("question", "?")})
    iters.sort(key=lambda i: (i["n"] is None, i["n"]))
    for pos, item in enumerate(iters):
        if finished or pos < len(iters) - 1:
            item["status"] = "done"
        else:
            item["status"] = "active"
    return iters


def _improvements(bundle: Path) -> list[dict]:
    """One entry per code-change proposal, newest evidence first.

    Candidate records carry the eval verdict; assessment proposals without
    a candidate record never reached eval (diff failed or round aborted).
    PR linkage arrives with the publish bridge (#66); until then every
    entry is honestly unfiled.
    """
    found: dict[str, dict] = {}
    for path in sorted(glob.glob(str(bundle / "assessments" / "candidates" / "*.json"))):
        d = _load_json(Path(path), {})
        if not isinstance(d, dict):
            continue
        pid = str(d.get("proposal_id", Path(path).stem))
        found[pid] = {"id": pid, "target": d.get("target", "?"),
                      "status": d.get("status", "?"),
                      "reason": str(d.get("detail", ""))[:300],
                      "pr_url": None, "merged": False}
    for path in sorted(glob.glob(str(bundle / "assessments" / "assess-*.json"))):
        d = _load_json(Path(path), {})
        if not isinstance(d, dict):
            continue
        for p in d.get("proposals", []) or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id", "?"))
            if pid in found:
                continue
            found[pid] = {"id": pid, "target": p.get("target", "?"),
                          "status": "not-evaluated",
                          "reason": str(p.get("change", ""))[:300],
                          "pr_url": None, "merged": False}
    return [found[k] for k in sorted(found)]


def _revised_tally(events: list[dict]) -> dict:
    kept = reverted = skipped = 0
    for ev in events:
        if ev.get("event") not in ("revised", "mid-revised"):
            continue
        detail = str(ev.get("detail", ""))
        for key in ("kept", "reverted", "skipped"):
            for part in detail.replace(",", " ").split():
                if part.startswith(key + "="):
                    try:
                        if key == "kept":
                            kept += int(part.split("=")[1])
                        elif key == "reverted":
                            reverted += int(part.split("=")[1])
                        else:
                            skipped += int(part.split("=")[1])
                    except ValueError:
                        pass
    return {"kept": kept, "reverted": reverted, "skipped": skipped}


def parse_bundle(bundle: str | Path) -> dict:
    """Bundle directory → dashboard row fields (no registry I/O)."""
    bundle = Path(bundle)
    manifest = _load_json(bundle / "manifest.json", {})
    run = _load_json(bundle / "run.json", {})
    spec = manifest.get("spec", {}) if isinstance(manifest, dict) else {}
    events = _journal_events(bundle)
    status = (manifest.get("status") or run.get("stopped") or "unknown"
              if isinstance(manifest, dict) else "unknown")
    finished = status not in ("unknown", "running", "")
    usage = run.get("usage", {}) if isinstance(run, dict) else {}
    record = {
        "run_id": manifest.get("run_id", "?") if isinstance(manifest, dict) else "?",
        "kind": manifest.get("kind", "?") if isinstance(manifest, dict) else "?",
        "status": status,
        "started_at": manifest.get("started_at") if isinstance(manifest, dict) else None,
        "ended_at": manifest.get("finished_at") if isinstance(manifest, dict) else None,
        "seed": spec.get("question", "?") if isinstance(spec, dict) else "?",
        "profile": spec.get("profile", "?") if isinstance(spec, dict) else "?",
        "maintenance": bool(spec.get("maintenance", False)) if isinstance(spec, dict) else False,
        "model": run.get("model", "?") if isinstance(run, dict) else "?",
        "usage": {"model_calls": usage.get("model_calls", 0),
                  "tokens": usage.get("tokens", 0)},
        "iterations": _iterations(bundle, finished),
        "improvements": _improvements(bundle),
        "tally": _revised_tally(events),
        "bundle": str(bundle),
        "log_files": [str(bundle / name) for name in
                      ("journal.jsonl", "ops.jsonl", "console.log")
                      if (bundle / name).exists()],
    }
    receipt = _load_json(Path(str(bundle) + ".receipt.json"), {})
    if isinstance(receipt, dict) and receipt:
        record["container"] = receipt.get("NAME")
        record["exit_code"] = receipt.get("RC")
        record["git_rev"] = receipt.get("GIT_REV")
    return record
