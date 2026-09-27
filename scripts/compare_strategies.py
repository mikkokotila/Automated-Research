#!/usr/bin/env python3
"""Three-arm retrieval-strategy comparison on scripted fixtures (#19).

Runs fixed / fixed+memory / bandit+memory arms over deterministic provider
and model doubles and seals a dated evidence bundle. Fixture mechanics
only: whatever the numbers, they cannot establish real research benefit,
so the standing decision recorded here is NO-GO for enabling beyond the
experiment (fixed policy retained) until live-gated evidence exists.

Usage:
    uv run --no-sync python scripts/compare_strategies.py [--out DIR] [--seed N]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from canary.strategy import (  # noqa: E402
    COMPARISON_VERSION, POLICY_IMPL, REWARD_VERSION, STRATEGY_VERSION,
    compare_three_arm,
)


def render_markdown(report: dict) -> str:
    lines = ["# Strategy comparison (scripted fixtures)", "",
             f"Comparison v{COMPARISON_VERSION} | policy {POLICY_IMPL} | "
             f"strategies v{STRATEGY_VERSION} | reward v{REWARD_VERSION} | "
             f"seed {report['seed']} | eps {report['epsilon']}", ""]
    for split in ("dev", "held"):
        lines += [f"## {split} ({', '.join(report[f'{split}_ids'])})", "",
                  "| arm | question | strategy | reward | status | model | provider |",
                  "|---|---|---|---|---|---|---|"]
        for arm in ("A", "B", "C"):
            for row in report[split][arm]["rows"]:
                lines.append(
                    f"| {arm} | {row['id']} | {row['strategy']} | {row['reward']:.3f} "
                    f"| {row['status']} | {row['counts']['model_calls']} "
                    f"| {row['counts']['provider_calls']} |")
        lines.append("")
    lines += ["## Held-out summary (N=2, raw values, no CI implied)", ""]
    for arm, summary in report["held_summary"].items():
        lines.append(f"- {arm}: rewards={summary['rewards']} "
                     f"mean={summary['mean']:.3f} range={summary['range']} "
                     f"strategies={summary['strategies']}")
    lines += ["", "## Costs", ""]
    for arm, costs in report["costs"].items():
        lines.append(f"- {arm}: {costs}")
    lines += ["", "## Disclaimers", ""]
    lines += [f"- {d}" for d in report["disclaimers"]]
    lines += ["", "## Decision",
              "",
              "**NO-GO for enabling beyond the experiment (fixed policy "
              "retained).** Scripted fixtures prove mechanics — updates "
              "apply, freezing holds, costs are counted, held-out runs "
              "frozen — and are inconclusive for real benefit by design. "
              "Enablement needs the live-gated protocol in the validation "
              "record, not this bundle.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scripted three-arm comparison.")
    ap.add_argument("--out", default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--epsilon", type=float, default=0.2)
    args = ap.parse_args(argv)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = Path(args.out) if args.out else ROOT / "validation" / day / "bandit"
    out.mkdir(parents=True, exist_ok=True)
    report = compare_three_arm(out / "runs", seed=args.seed, epsilon=args.epsilon)
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out / "report.md").write_text(render_markdown(report))
    for arm, summary in report["held_summary"].items():
        print(f"held {arm}: mean={summary['mean']:.3f} range={summary['range']} "
              f"strategies={summary['strategies']}")
    print(f"costs: {report['costs']}")
    print(f"wrote {out / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
