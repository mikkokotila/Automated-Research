"""Run registry: every run gets a name, a brief, and one dashboard row.

Registry lives at runs/registry.json (local-only, gitignored). The daemon
and launcher register container runs at start/finish; adopt() takes in
older bundles. Writes are atomic (tmp + replace); reads tolerate a
missing file. Bundle parsing is defensive: missing files yield empty
lists and "unknown" fields, never an exception.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
# Overridable so tests and alternate checkouts isolate their registry; the
# daemon exports it for launcher children so hooks land in the same file.
REGISTRY = Path(os.environ.get("CANARY_RUNS_REGISTRY", ROOT / "runs" / "registry.json"))
# Operator env passthrough for launches (advanced mode; default runs need
# none of it). Daemon-managed vars (OUT, RUN_*, registry) are never passed.
LAUNCH_ENV_KEYS = ("CANARY_STAGE", "CANARY_TIMEOUT_S", "CANARY_WHEELS")
LAUNCH_ENV_MAXLEN = 2000


def sanitize_launch_env(env) -> dict:
    """Allowlisted launch env or ValueError; the launcher rechecks semantics."""
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ValueError("env must be an object of KEY: value strings")
    clean = {}
    for key, value in env.items():
        if key not in LAUNCH_ENV_KEYS:
            raise ValueError(f"env key not allowed: {key!r}")
        if not isinstance(value, str) or len(value) > LAUNCH_ENV_MAXLEN:
            raise ValueError(f"env value for {key} must be a string <= 2000 chars")
        clean[key] = value
    return clean


def ambient_launch_env() -> dict:
    """Allowlisted keys from this process's environment (launcher hook)."""
    return {k: v for k in LAUNCH_ENV_KEYS if (v := os.environ.get(k))}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(path: str | Path = REGISTRY) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def save(rows: list[dict], path: str | Path = REGISTRY) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def upsert(row: dict, path: str | Path = REGISTRY) -> dict:
    rows = [r for r in load(path) if r.get("key") != row["key"]]
    rows.append(row)
    rows.sort(key=lambda r: str(r.get("started_at", "")))
    save(rows, path)
    return row


def get(key: str, path: str | Path = REGISTRY) -> dict | None:
    return next((r for r in load(path) if r.get("key") == key), None)


def register_start(key: str, name: str, brief: str, kind: str = "?",
                   bundle: str = "", container: str = "", launch: str = "",
                   path: str | Path = REGISTRY,
                   launch_argv: list[str] | None = None,
                   launch_env: dict | None = None) -> dict:
    return upsert({"key": key, "name": name, "brief": brief, "run_id": "?",
                   "kind": kind, "status": "running", "started_at": utcnow(),
                   "ended_at": None, "bundle": bundle, "container": container,
                   "launch": launch, "launch_argv": list(launch_argv or []),
                   "launch_env": dict(launch_env or {}),
                   "seed": "?", "profile": "?",
                   "maintenance": False, "model": "?", "usage": {},
                   "iterations": [], "improvements": [], "tally": {}}, path)


_NON_TERMINAL = (None, "unknown", "running", "paused", "")


def register_finish(key: str, status: str = "done", bundle: str = "",
                    container: str = "", path: str | Path = REGISTRY) -> dict | None:
    rows = load(path)
    current = next((r for r in rows if r.get("key") == key), None)
    if current is None:
        return None
    if current.get("status") not in _NON_TERMINAL:
        return current  # terminal rows are immutable; late writers no-op
    target = Path(bundle or current.get("bundle", ""))
    if target.is_dir():
        parsed = parse_bundle(target)
        current.update({k: v for k, v in parsed.items()
                        if k != "bundle" and v not in (None, "?", {})})
        current["bundle"] = str(target)
    if status != "done" or current.get("status") in _NON_TERMINAL:
        current["status"] = status  # failed/interrupted win; done defers to bundle
    current["ended_at"] = utcnow()
    if container:
        current["container"] = container
    return upsert(current, path)


def adopt_bundle(bundle: str | Path, name: str = "", brief: str = "",
                 path: str | Path = REGISTRY) -> dict:
    bundle = Path(bundle)
    parsed = parse_bundle(bundle)
    row = {"key": bundle.name,
           "name": name or f"{bundle.name} — {parsed['kind']} run",
           "brief": brief or f"Seed: {parsed['seed']} (backfilled, no brief recorded)",
           "launch": "", "container": parsed.pop("container", None)}
    row.update(parsed)
    return upsert(row, path)


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def journal_events(bundle: str | Path) -> list[dict]:
    events = []
    try:
        lines = (Path(bundle) / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def ops_events(bundle: str | Path) -> list[dict]:
    events = []
    try:
        lines = (Path(bundle) / "ops.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
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
        item["status"] = "done" if (finished or pos < len(iters) - 1) else "active"
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
    tally = {"kept": 0, "reverted": 0, "skipped": 0}
    for ev in events:
        if ev.get("event") not in ("revised", "mid-revised"):
            continue
        for part in str(ev.get("detail", "")).replace(",", " ").split():
            if "=" not in part:
                continue
            key, _, value = part.partition("=")
            if key in tally:
                try:
                    tally[key] += int(value)
                except ValueError:
                    pass
    return tally


def parse_bundle(bundle: str | Path) -> dict:
    """Bundle directory → dashboard row fields (no registry I/O)."""
    bundle = Path(bundle)
    manifest = _load_json(bundle / "manifest.json", {})
    run = _load_json(bundle / "run.json", {})
    if not isinstance(manifest, dict):
        manifest = {}
    if not isinstance(run, dict):
        run = {}
    spec = manifest.get("spec", {})
    if not isinstance(spec, dict):
        spec = {}
    events = journal_events(bundle)
    status = manifest.get("status") or run.get("stopped") or "unknown"
    finished = status not in ("unknown", "running", "")
    usage = run.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    record = {
        "run_id": manifest.get("run_id", "?"),
        "kind": manifest.get("kind", "?"),
        "status": status,
        "started_at": manifest.get("started_at"),
        "ended_at": manifest.get("finished_at"),
        "seed": spec.get("question", "?"),
        "profile": spec.get("profile", "?"),
        "maintenance": bool(spec.get("maintenance", False)),
        "model": run.get("model", "?"),
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
        if not manifest:
            # Exec probes ship no canary bundle; the receipt is the record.
            record["kind"] = "exec"
            rc = receipt.get("RC")
            record["status"] = ("done" if rc == "0" else
                                "failed" if rc not in (None, "") else "unknown")
            for field, epoch in (("started_at", "STARTED"), ("ended_at", "FINISHED")):
                try:
                    record[field] = datetime.fromtimestamp(
                        int(receipt[epoch]), tz=timezone.utc).isoformat()
                except (KeyError, TypeError, ValueError):
                    pass
    return record
