"""Operator CLI for the evaluation gate.

Baseline:  python -m evalpack.run --baseline --repo . --out evalpack/baseline.json
Decide:    python -m evalpack.run --repo . --base <rev> --diff candidate.diff \
               --expect-fix rel-weak --out /tmp/decision/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import controller


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval-gate", description="Run the evaluation gate.")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--diff", default=None, help="candidate unified diff file")
    ap.add_argument("--expect-fix", default="", help="comma-separated fixture ids")
    ap.add_argument("--baseline", action="store_true", help="record baseline, no decision")
    ap.add_argument("--out", required=True)
    ap.add_argument("--log", default=None, help="acceptance access log path")
    args = ap.parse_args(argv)
    import subprocess as _sp

    rev = _sp.run(["git", "rev-parse", args.base], cwd=args.repo, capture_output=True,
                  text=True)
    args.base = rev.stdout.strip() if rev.returncode == 0 else args.base
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dev = controller.load_set("dev")
    acceptance = controller.load_set("acceptance", args.log, reason="gate-run")
    fixtures = dev + acceptance
    diff = Path(args.diff).read_text(encoding="utf-8") if args.diff else None
    tree = controller.build_tree(args.repo, args.base, diff)
    try:
        results = controller.run_pack(tree, fixtures)
    finally:
        controller.teardown(tree)
    if args.baseline:
        payload = {"pack_version": controller.PACK_VERSION,
                   "thresholds": controller.thresholds_hash(), "base_rev": args.base,
                   "results": results}
        (out / "baseline.json" if out.is_dir() else out).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"baseline: {sum(r['pass'] for r in results.values())}/{len(results)} pass")
        return 0
    base_path = controller.PACK_DIR / "baseline.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))["results"]
    expected = [f for f in args.expect_fix.split(",") if f]
    decision = controller.decide(base, results, expected)
    (out / "decision.json").write_text(json.dumps(decision, indent=2) + "\n",
                                       encoding="utf-8")
    (out / "decision.md").write_text(
        controller.render_report(decision, base, results, args.base,
                                 args.diff or "worktree"), encoding="utf-8")
    (out / "candidate.json").write_text(json.dumps(results, indent=2) + "\n",
                                        encoding="utf-8")
    print(f"decision: {decision['verdict']}")
    return 0 if decision["verdict"] == "accept" else 1


if __name__ == "__main__":
    raise SystemExit(main())
