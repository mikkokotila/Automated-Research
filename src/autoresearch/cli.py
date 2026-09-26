"""CLI: question in, cited review out."""

from __future__ import annotations

import argparse
import sys

import httpx

from . import analysis, data as datamod, loop as loopmod, modeling, rank, report, retrieval, synthesize
from .muse_client import MuseClient
from .spec import ResearchSpec


def review(question: str, max_papers: int, year_from: int | None, out_dir: str) -> str:
    spec = ResearchSpec(question=question, max_papers=max_papers, year_from=year_from)
    with httpx.Client(headers={"User-Agent": "Automated-Research/0.1"}) as http:
        papers = retrieval.retrieve(spec, http)
    if not papers:
        raise RuntimeError("no papers found for this question")
    top = rank.rerank(spec.question, papers, spec.max_papers)
    synth = synthesize.synthesize(spec.question, top, MuseClient())
    path = report.write_bundle(out_dir, spec, top, synth)
    return str(path / "review.md")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autoresearch", description="Automated researcher, milestone 1.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review", help="run a cited literature review")
    r.add_argument("question", help="research question in plain words")
    r.add_argument("--max-papers", type=int, default=10)
    r.add_argument("--year-from", type=int, default=None)
    r.add_argument("--out", default="./out")
    a = sub.add_parser("analyze", help="test a hypothesis against a CSV file")
    a.add_argument("csv", help="path to CSV data file")
    a.add_argument("--target", required=True, help="target column to predict")
    a.add_argument("--question", default="", help="research question in plain words")
    a.add_argument("--out", default="./out")
    lo = sub.add_parser("loop", help="autonomous run: answers become next questions")
    lo.add_argument("question", help="seed research question in plain words")
    lo.add_argument("--csv", default=None, help="optional dataset for analyze iterations")
    lo.add_argument("--target", default=None, help="target column (with --csv)")
    lo.add_argument("--max-iterations", type=int, default=3)
    lo.add_argument("--max-papers", type=int, default=5)
    lo.add_argument("--out", default="./out")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "review":
            path = review(args.question, args.max_papers, args.year_from, args.out)
        elif args.cmd == "analyze":
            path = analyze(args.csv, args.target, args.question, args.out)
        else:
            path = loop(args.question, args.csv, args.target, args.max_iterations, args.max_papers, args.out)
    except Exception as e:  # honest failure, never fake output
        print(f"autoresearch: error: {e}", file=sys.stderr)
        return 1
    print(path)
    return 0


def loop(question: str, csv: str | None, target: str | None, max_iterations: int, max_papers: int, out_dir: str) -> str:
    res = loopmod.run_loop(question, csv, target, max_iterations, max_papers, MuseClient())
    path = report.write_loop_bundle(out_dir, question, res)
    return str(path / "synthesis.md")


def analyze(csv: str, target: str, question: str, out_dir: str) -> str:
    df = datamod.load_csv(csv)
    prep = datamod.prepare(df, target)
    res = modeling.run(prep)
    q = question.strip() or f"what predicts {target}?"
    findings = analysis.narrate(q, prep, res, MuseClient())
    path = report.write_analysis_bundle(out_dir, q, csv, target, prep, res, findings)
    return str(path / "analysis.md")


if __name__ == "__main__":
    raise SystemExit(main())
