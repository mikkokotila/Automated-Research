#!/usr/bin/env python3
"""Run registry: every run gets a name, a brief, and one row in the dashboard.

Registry lives at runs/registry.json (local-only, gitignored). The launcher
registers container runs at start/finish; backfill/scan commands adopt older
bundles. Writes are atomic (tmp + replace); reads tolerate a missing file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "runs" / "registry.json"
sys.path.insert(0, str(ROOT / "scripts"))

from run_record import parse_bundle  # noqa: E402


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def load() -> list[dict]:
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save(rows: list[dict]) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, REGISTRY)


def upsert(row: dict) -> dict:
    rows = [r for r in load()
            if not isinstance(r, dict) or r.get("key") != row["key"]]
    rows.append(row)
    rows.sort(key=lambda r: str(r.get("started_at", "")))
    save(rows)
    return row


def cmd_start(args) -> int:
    row = {"key": args.key, "name": args.name, "brief": args.brief,
           "run_id": "?", "kind": args.kind, "status": "running",
           "started_at": _utcnow(), "ended_at": None,
           "bundle": args.bundle, "container": args.container,
           "launch": args.launch, "seed": "?", "profile": "?",
           "maintenance": False, "model": "?", "usage": {},
           "iterations": [], "improvements": [], "tally": {}}
    upsert(row)
    print(json.dumps({"key": row["key"], "status": "running"}))
    return 0


def cmd_finish(args) -> int:
    rows = load()
    current = next((r for r in rows if isinstance(r, dict) and r.get("key") == args.key), None)
    if current is None:
        print(f"no registry entry for {args.key}", file=sys.stderr)
        return 1
    bundle = Path(args.bundle or current.get("bundle", ""))
    if bundle.is_dir():
        parsed = parse_bundle(bundle)
        current.update({k: v for k, v in parsed.items()
                        if k not in ("bundle",) and v not in (None, "?", {})})
        current["bundle"] = str(bundle)
    current["status"] = args.status
    current["ended_at"] = _utcnow()
    if args.container:
        current["container"] = args.container
    upsert(current)
    print(json.dumps({"key": current["key"], "status": current["status"]}))
    return 0


def cmd_adopt(args) -> int:
    """Adopt an existing bundle dir as a historical row (backfill)."""
    bundle = Path(args.bundle)
    if not bundle.is_dir():
        print(f"not a directory: {bundle}", file=sys.stderr)
        return 1
    parsed = parse_bundle(bundle)
    name = args.name or f"{bundle.name} — {parsed['kind']} run"
    brief = args.brief or f"Seed: {parsed['seed']} (backfilled, no brief recorded)"
    row = {"key": bundle.name, "name": name, "brief": brief,
           "launch": "", "container": parsed.pop("container", None)}
    row.update(parsed)
    upsert(row)
    print(json.dumps({"key": row["key"], "status": row["status"]}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="register_run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--key", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--brief", required=True)
    s.add_argument("--kind", default="?")
    s.add_argument("--bundle", default="")
    s.add_argument("--container", default="")
    s.add_argument("--launch", default="")
    f = sub.add_parser("finish")
    f.add_argument("--key", required=True)
    f.add_argument("--status", default="done")
    f.add_argument("--bundle", default="")
    f.add_argument("--container", default="")
    a = sub.add_parser("adopt")
    a.add_argument("--bundle", required=True)
    a.add_argument("--name", default="")
    a.add_argument("--brief", default="")
    args = ap.parse_args(argv)
    return {"start": cmd_start, "finish": cmd_finish, "adopt": cmd_adopt}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
