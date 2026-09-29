"""Snag harvester: bundle -> grouped improvement log."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import harvest_snags as hs


def _bundle(tmp_path: Path) -> Path:
    b = tmp_path / "b1"
    (b / "iterations").mkdir(parents=True)
    (b / "assessments" / "candidates").mkdir(parents=True)
    journal = [
        {"ts": "t", "phase": "review", "event": "provider-failed", "seq": 1,
         "detail": "openalex: HTTPStatusError: 429 for url 'http://g/v1/sources/openalex'"},
        {"ts": "t", "phase": "review", "event": "coverage-warning", "seq": 2,
         "detail": "degraded coverage: openalex failed; 25 papers from remaining sources only"},
        {"ts": "t", "phase": "review", "event": "coverage-footer-appended", "seq": 3,
         "detail": "model omitted the footer; guard appended it"},
        {"ts": "t", "phase": "cycle", "event": "done", "seq": 4,
         "detail": "iters=1 stopped=budget_exhausted"},
    ]
    (b / "journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in journal) + "\n", encoding="utf-8")
    (b / "iterations" / "iter1-review.md").write_text(
        "# R\n\n_Papers reviewed: 15 | Model: m | Cited: 4/15_\n", encoding="utf-8")
    (b / "assessments" / "candidates" / "p1.json").write_text(json.dumps({
        "proposal_id": "p1", "target": "src/canary/x.py", "status": "rejected",
        "change": "do the thing", "detail": "checks failed with exit 1",
    }), encoding="utf-8")
    (b / "manifest.json").write_text(json.dumps(
        {"status": "budget_exhausted"}), encoding="utf-8")
    (b / "run.json").write_text(json.dumps(
        {"stopped": "budget_exhausted", "unanswered": ["why is the sky blue?"]}),
        encoding="utf-8")
    return b


def test_harvest_groups_all_snag_kinds(tmp_path):
    snags = hs.harvest(_bundle(tmp_path))
    by_kind = {}
    for s in snags:
        by_kind.setdefault(s["kind"], []).append(s["title"])
    assert set(by_kind) == {"provider", "coverage", "precision",
                            "maintenance", "carryover"}
    assert any("openalex" in t for t in by_kind["provider"])
    assert len(by_kind["coverage"]) == 2  # warning + appended-footer omission
    assert any("4/15" in t for t in by_kind["precision"])
    assert any("p1 rejected" in t for t in by_kind["maintenance"])
    assert any("1 unanswered" in t for t in by_kind["carryover"])


def test_harvest_empty_bundle_reports_no_snags(tmp_path):
    b = tmp_path / "clean"
    b.mkdir()
    (b / "journal.jsonl").write_text(
        json.dumps({"ts": "t", "phase": "cycle", "event": "done",
                    "detail": "iters=1 stopped=converged"}) + "\n", encoding="utf-8")
    (b / "manifest.json").write_text(json.dumps({"status": "converged"}),
                                     encoding="utf-8")
    assert hs.harvest(b) == []
    assert "No snags" in hs.render_md("clean", [])


def test_main_writes_snags_md(tmp_path):
    rc = hs.main([str(_bundle(tmp_path))])
    assert rc == 0
    md = (tmp_path / "b1" / "SNAGS.md").read_text(encoding="utf-8")
    assert md.startswith("# Snag log: b1") and "## provider (1)" in md
