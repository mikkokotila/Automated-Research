#!/usr/bin/env python3
"""Adopt every container-out bundle into the run registry (one-time history).

Curated names/briefs for the runs that matter; probes get honest auto rows.
Rerunnable: keys already in the registry are skipped unless --force.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from canary import runs

CURATED = {
    "demo52": ("demo52a — first unmanned maintenance",
               "First maintenance run through the interim gate. Gate authorized; "
               "both assessments failed on phantom targets (model guessed paths)."),
    "demo52b": ("demo52b — inventory grounding",
                "Proposal inventory added. Proposals grounded and actionable; "
               "post-run round vetoed by the run's own out/ writes (record-dir bug)."),
    "demo52c": ("demo52c — veto fix, honest rejections",
                "Record-dir exclusion + faithful eval trees. 3 proposals, 1 applicable; "
                "the jitter patch broke 2 retry-contract tests and was rejected. kept=0."),
    "demo52d": ("demo52d — retry logic live",
                "Applicable-diff retry with feedback. 4 malformed diffs exhausted retries; "
                "revision spend cap vetoed post-run. kept=0."),
    "hook-probe": ("hook probe — registry wiring",
                   "Launcher registry-hook verification (exec true)."),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backfill_runs")
    ap.add_argument("--force", action="store_true",
                    help="re-adopt keys already in the registry")
    args = ap.parse_args(argv)
    root = runs.ROOT / "container-out"
    existing = {r.get("key") for r in runs.load()}
    adopted, skipped = 0, 0
    for bundle in sorted(root.iterdir()):
        if not bundle.is_dir():
            continue
        if bundle.name in existing and not args.force:
            skipped += 1
            continue
        name, brief = CURATED.get(bundle.name, ("", ""))
        row = runs.adopt_bundle(bundle, name, brief)
        adopted += 1
        print(f"adopted {row['key']}: {row['name']} [{row['status']}]")
    print(f"done: {adopted} adopted, {skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
