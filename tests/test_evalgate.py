"""Build 14: the gate holds against broken, forged, and silent candidates."""
import json
import subprocess
from pathlib import Path

import pytest

from evalpack import controller
from canary.changeset import ChangesetError

REPO = controller.PACK_DIR.parent
_FIXTURES = Path(__file__).parent / "fixtures" / "evalgate"


def base_rev() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                          text=True, check=True).stdout.strip()


def dev_fixture(fid: str) -> dict:
    pack = json.loads((controller.PACK_DIR / "dev.json").read_text(encoding="utf-8"))
    return next(f for f in pack["fixtures"] if f["id"] == fid)


def apply_in_tree(tree, diff: str) -> None:
    r = subprocess.run(["git", "apply", "-"], input=diff.encode(), cwd=tree,
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode()[:300]


BREAK_KEYWORDS = (_FIXTURES / "bk.diff").read_text()

FORGE_ADEQUATE = (_FIXTURES / "forge.diff").read_text()

NOOP_COMMENT = (_FIXTURES / "noop.diff").read_text()

BREAK_MATCHED = (_FIXTURES / "breakm.diff").read_text()

FIX_MATCHED = (_FIXTURES / "fixm.diff").read_text()


# --- isolation and scoring ---


def test_isolated_workers_score_dev_subset(tmp_path):
    tree = controller.build_tree(REPO, base_rev(), None, tmp_path / "tree")
    try:
        results = controller.run_pack(tree, [dev_fixture("rel-weak"),
                                             dev_fixture("budget-cap")])
    finally:
        controller.teardown(tree)
    assert results["rel-weak"]["pass"] and results["budget-cap"]["pass"]
    assert not (tmp_path / "tree").exists()


def test_worker_env_is_scrubbed_and_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("CANARY_GATE_TOKEN", "secret")
    monkeypatch.setenv("MUSE_API_KEY", "secret")
    env = controller.worker_env(tmp_path)
    assert env["PYTHONHASHSEED"] == "0"
    assert "secret" not in " ".join(env.values())
    assert "CANARY_GATE_TOKEN" not in env and "MUSE_API_KEY" not in env


def test_worker_tree_contains_no_expectations(tmp_path):
    tree = controller.build_tree(REPO, base_rev(), None, tmp_path / "tree")
    try:
        shipped = {p.name for p in (tree / "evalpack").iterdir()}
        assert shipped == {"__init__.py", "worker.py"}
    finally:
        controller.teardown(tree)


def test_malformed_and_unknown_tasks_fail_closed(tmp_path):
    tree = controller.build_tree(REPO, base_rev(), None, tmp_path / "tree")
    try:
        assert controller.run_task(tree, "nope", {})["ok"] is False
        bad = controller.run_task(tree, "relevance", {"wrong": "shape"})
        assert bad["ok"] is False and "Error" in bad["error"]
    finally:
        controller.teardown(tree)


def test_timeout_fails_closed(tmp_path):
    tree = controller.build_tree(REPO, base_rev(), None, tmp_path / "tree")
    try:
        detail = controller.run_task(tree, "leakage", {"seed": 0}, timeout_s=1)
        assert detail["ok"] is False and "timeout" in detail["error"]
    finally:
        controller.teardown(tree)


def test_gate_and_test_targets_reject_candidates():
    with pytest.raises(ChangesetError, match="protected area"):
        controller.build_tree(REPO, base_rev(),
                              BREAK_KEYWORDS.replace("src/canary/rank.py",
                                                     "evalpack/worker.py"))
    with pytest.raises(ChangesetError, match="protected area"):
        controller.build_tree(REPO, base_rev(),
                              BREAK_KEYWORDS.replace("src/canary/rank.py",
                                                     "tests/test_x.py"))


# --- verdicts ---


def test_broken_candidate_regresses_and_rejects(tmp_path):
    tree = controller.build_tree(REPO, base_rev(), BREAK_KEYWORDS, tmp_path / "tree")
    try:
        cand = controller.run_pack(tree, [dev_fixture("rel-weak"),
                                          dev_fixture("rel-strong")])
    finally:
        controller.teardown(tree)
    assert not cand["rel-weak"]["pass"]  # vacuous adequate != frozen weak
    base = {"rel-weak": {"pass": True}, "rel-strong": {"pass": True}}
    decision = controller.decide(base, cand, ["rel-weak"])
    assert decision["verdict"] == "reject"
    assert any("regression" in r or "not failing" in r for r in decision["reasons"])


def test_forged_success_rejected_by_controller_truth(tmp_path):
    tree = controller.build_tree(REPO, base_rev(), FORGE_ADEQUATE, tmp_path / "tree")
    try:
        cand = controller.run_pack(tree, [dev_fixture("rel-weak")])
    finally:
        controller.teardown(tree)
    assert cand["rel-weak"]["output"]["verdict"] == "adequate"  # the forgery ran
    assert not cand["rel-weak"]["pass"]  # controller truth still says weak
    decision = controller.decide({"rel-weak": {"pass": True}}, cand, [])
    assert decision["verdict"] == "reject"


def test_silent_candidate_rejected_for_no_benefit(tmp_path):
    tree = controller.build_tree(REPO, base_rev(), NOOP_COMMENT, tmp_path / "tree")
    try:
        cand = controller.run_pack(tree, [dev_fixture("rel-weak")])
    finally:
        controller.teardown(tree)
    assert cand["rel-weak"]["pass"]
    decision = controller.decide({"rel-weak": {"pass": True}}, cand, [])
    assert decision["verdict"] == "reject"
    assert any("no predeclared benefit" in r for r in decision["reasons"])


def test_corrective_candidate_accepted_on_defective_copy(tmp_path):
    weak = dev_fixture("rel-weak")
    strong = dev_fixture("rel-strong")
    tree = controller.build_tree(REPO, base_rev(), BREAK_MATCHED, tmp_path / "defect")
    try:
        defective = controller.run_pack(tree, [weak, strong])
        assert not defective["rel-strong"]["pass"]  # the predeclared defect
        assert not defective["rel-weak"]["pass"]  # cover 0 != frozen cover 2
        apply_in_tree(tree, FIX_MATCHED)
        fixed = controller.run_pack(tree, [weak, strong])
        assert fixed["rel-weak"]["pass"] and fixed["rel-strong"]["pass"]
    finally:
        controller.teardown(tree)
    decision = controller.decide(defective, fixed, ["rel-strong"])
    assert decision["verdict"] == "accept", decision["reasons"]


def test_decide_rules_without_workers():
    base = {"a": {"pass": True}, "b": {"pass": False}}
    cand = {"a": {"pass": True}, "b": {"pass": True}}
    assert controller.decide(base, cand, ["b"])["verdict"] == "accept"
    assert controller.decide(base, cand, [])["verdict"] == "reject"
    assert controller.decide(base, cand, ["zzz"])["verdict"] == "reject"
    assert controller.decide(base, cand, ["a"])["verdict"] == "reject"
    cand2 = {"a": {"pass": False}, "b": {"pass": True}}
    d = controller.decide(base, cand2, ["b"])
    assert d["verdict"] == "reject" and d["regressions"] == ["a"]
    report = controller.render_report(d, base, cand2, "abc123", "candidate.diff")
    assert "reject" in report and "| a | pass | FAIL |" in report


def test_acceptance_access_is_logged(tmp_path):
    log = tmp_path / "access.log"
    controller.load_set("dev")
    assert not log.exists()
    controller.load_set("acceptance", log, reason="test-run")
    entry = json.loads(log.read_text(encoding="utf-8"))
    assert entry["set"] == "acceptance" and entry["reason"] == "test-run"
    assert entry["pack_version"] == controller.PACK_VERSION


def test_full_pack_reproducible(tmp_path):
    dev = controller.load_set("dev")
    acceptance = controller.load_set("acceptance")
    fixtures = dev + acceptance
    runs = []
    for i in range(2):
        tree = controller.build_tree(REPO, base_rev(), None, tmp_path / f"t{i}")
        try:
            runs.append(controller.run_pack(tree, fixtures))
        finally:
            controller.teardown(tree)
    assert runs[0] == runs[1]
    assert all(r["pass"] for r in runs[0].values())
