#!/usr/bin/env python3
"""Run registry CLI (thin wrapper over canary.runs; used by the launcher)."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from canary import runs


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
    s.add_argument("--launch-argv", default="",
                   help="JSON argv array; rerun prefers it over --launch")
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
    if args.cmd == "start":
        try:
            argv = json.loads(args.launch_argv) if args.launch_argv else None
        except (ValueError, TypeError):
            argv = None
        if not (isinstance(argv, list)
                and all(isinstance(a, str) for a in argv)):
            argv = None
        row = runs.register_start(args.key, args.name, args.brief, args.kind,
                                  args.bundle, args.container, args.launch,
                                  launch_argv=argv)
        print(json.dumps({"key": row["key"], "status": "running"}))
        return 0
    if args.cmd == "finish":
        row = runs.register_finish(args.key, args.status, args.bundle, args.container)
        if row is None:
            print(f"no registry entry for {args.key}", file=sys.stderr)
            return 1
        print(json.dumps({"key": row["key"], "status": row["status"]}))
        return 0
    row = runs.adopt_bundle(args.bundle, args.name, args.brief)
    print(json.dumps({"key": row["key"], "status": row["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
