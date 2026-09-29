#!/usr/bin/env python3
"""Harvest snag log from a sealed run bundle (post-run improvement input).

Reads journal.jsonl, iterations/, assessments/candidates/, manifest.json and
emits grouped snags: provider failures, coverage gaps, validator rejections,
low citation yield, failed maintenance candidates, and unanswered carry-over.

Usage: harvest_snags.py BUNDLE [--json] [--out DIR]
Writes SNAGS.md (or snags.json) into the bundle dir by default.
"""
import glob
import json
import re
import sys
from pathlib import Path

CITED_RE = re.compile(r"Cited:\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
ENGAGED_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+engage\s*>=\s*(\d+)",
                        re.IGNORECASE)


def _read_jsonl(path: Path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def harvest(bundle: Path) -> list[dict]:
    """Bundle dir -> snag dicts {kind, title, evidence[]} (stable order)."""
    bundle = Path(bundle)
    snags: list[dict] = []
    journal = _read_jsonl(bundle / "journal.jsonl")
    by_event: dict[str, list[str]] = {}
    for ev in journal:
        by_event.setdefault(str(ev.get("event", "?")), []).append(
            str(ev.get("detail", ""))[:220])

    def add(kind: str, title: str, evidence: list[str]):
        if evidence:
            snags.append({"kind": kind, "title": title,
                          "evidence": evidence[:8]})

    # 1. provider failures (retrieval-side), grouped by provider prefix
    prov: dict[str, list[str]] = {}
    for d in by_event.get("provider-failed", []):
        prov.setdefault(d.split(":")[0][:40] or "unknown", []).append(d)
    for name, ds in sorted(prov.items()):
        add("provider", f"provider failed {len(ds)}x: {name}", ds)

    trans = [d for d in by_event.get("provider-blocked", [])]
    add("provider", f"model provider blocked the run ({len(trans)}x)", trans)

    # 2. coverage gaps
    add("coverage", "degraded coverage warnings",
        by_event.get("coverage-warning", []))
    add("coverage", "model omitted the incomplete-coverage footer",
        by_event.get("coverage-footer-missing", []) +
        by_event.get("coverage-footer-appended", []))

    # 3. validator rejections (synthesis quality signal)
    add("validation", "validator rejected claims",
        by_event.get("claim-rejected", []))

    # 4. retrieval precision (engaged/candidates from the retrieval
    #    report). Cited/reviewed is model selectivity, not retrieval
    #    quality: selective citation of an engaged pool is correct (#101).
    prec_rows: list[tuple[str, int, int, int | None, bool]] = []
    for row in _read_jsonl(bundle / "retrieval.jsonl"):
        prec = (row.get("report") or {}).get("precision") or {}
        try:
            engaged, candidates = int(prec["engaged"]), int(prec["candidates"])
        except (KeyError, TypeError, ValueError):
            continue
        need = prec.get("min_terms")
        prec_rows.append((str(row.get("question", "?"))[:80], engaged,
                         candidates, need if isinstance(need, int) else None,
                         bool(prec.get("fallback_unfiltered"))))
    if not prec_rows:
        # legacy bundles predate retrieval.jsonl; the journal carries
        # the same numbers as precision-filter events
        for d in by_event.get("precision-filter", []):
            m = ENGAGED_RE.search(d)
            if m:
                prec_rows.append(("?", int(m.group(1)), int(m.group(2)),
                                 int(m.group(3)),
                                 "fallback to unfiltered" in d))
    for label, engaged, candidates, need, fallback in prec_rows:
        ratio = (engaged / candidates) if candidates else 0.0
        if candidates and ratio < 0.5:
            need_txt = f" >= {need} question terms" \
                if need is not None else ""
            add("precision",
                f"retrieval precision low for '{label}': "
                f"{engaged}/{candidates} engaged "
                f"({ratio:.0%} below 50% bar)"
                + ("; fallback to unfiltered top-N" if fallback else ""),
                [f"engaged {engaged} of {candidates} candidates{need_txt}"])

    # 4b. model citation behavior (secondary signal): only total
    #     non-citation is a snag; anything else is legitimate selectivity
    for md in sorted(glob.glob(str(bundle / "iterations" / "iter*-review.md"))):
        text = Path(md).read_text(encoding="utf-8", errors="replace")[:800]
        m = CITED_RE.search(text)
        if m:
            cited, total = int(m.group(1)), int(m.group(2))
            if total and not cited:
                add("model",
                    f"{Path(md).stem}: cited none of {total} reviewed papers",
                    [f"cited 0 of {total} reviewed papers"])

    # 5. maintenance candidates that did not land
    for cand in sorted(glob.glob(str(bundle / "assessments" / "candidates" / "*.json"))):
        d = _read_json(Path(cand)) or {}
        if d.get("status") == "kept":
            continue
        add("maintenance",
            f"{d.get('proposal_id', Path(cand).stem)} "
            f"{d.get('status', '?')}: {str(d.get('target', '?'))}",
            [f"change: {str(d.get('change', ''))[:200]}",
             f"detail: {str(d.get('detail', ''))[:200]}"])

    # 6. check failures
    fails = [d for d in by_event.get("checks", []) if "rc=0" not in d]
    add("checks", "eval-gate check failures", fails)
    add("maintenance", "diff generation failures",
        by_event.get("diff-failed", []) + by_event.get("revise-failed", []))

    # 7. stop state + carry-over
    man = _read_json(bundle / "manifest.json") or {}
    run = _read_json(bundle / "run.json") or {}
    status = man.get("status") or run.get("stopped") or "?"
    unanswered = list(run.get("unanswered") or [])
    if unanswered:
        add("carryover",
            f"run stopped={status} with {len(unanswered)} unanswered question(s)",
            unanswered[:8])
    elif status not in ("converged", "?", "done"):
        add("carryover", f"run stopped={status} (no carry-over recorded)", [status])
    return snags


def render_md(bundle_name: str, snags: list[dict]) -> str:
    lines = [f"# Snag log: {bundle_name}", ""]
    if not snags:
        lines.append("No snags harvested.")
        return "\n".join(lines) + "\n"
    by_kind: dict[str, list[dict]] = {}
    for s in snags:
        by_kind.setdefault(s["kind"], []).append(s)
    for kind, items in by_kind.items():
        lines.append(f"## {kind} ({len(items)})")
        lines.append("")
        for s in items:
            lines.append(f"- {s['title']}")
            for e in s["evidence"][:4]:
                lines.append(f"  - evidence: {e[:200]}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    as_json = "--json" in argv
    if not args:
        print("usage: harvest_snags.py BUNDLE [--json] [--out DIR]",
              file=sys.stderr)
        return 2
    bundle = Path(args[0])
    if not (bundle / "journal.jsonl").exists():
        print(f"not a bundle (no journal.jsonl): {bundle}", file=sys.stderr)
        return 2
    out_dir = bundle
    if "--out" in argv:
        out_dir = Path(argv[argv.index("--out") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
    snags = harvest(bundle)
    if as_json:
        (out_dir / "snags.json").write_text(
            json.dumps(snags, indent=1) + "\n", encoding="utf-8")
    else:
        (out_dir / "SNAGS.md").write_text(
            render_md(bundle.name, snags), encoding="utf-8")
    print(f"{len(snags)} snags -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
