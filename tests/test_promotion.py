"""Build 15: transactional evaluation, promotion, rollback, and recovery."""
import json
import subprocess

import pytest

from canary import promote as promotemod
from canary import revise as revmod
from canary.assess import AssessmentDoc, Proposal
from canary.journal import Journal
from canary.promote import PromotionError, PromotionHalt, Store, _CrashSim


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """Explicit in-process trust seam; git repo for patch application."""
    monkeypatch.setattr(revmod, "require_revision_trust", lambda: None)
    r = tmp_path / "repo"
    (r / "src" / "canary").mkdir(parents=True)
    (r / "src" / "canary" / "foo.py").write_text("X = 1\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=r, capture_output=True, check=True)
    return r


def prop(pid="p1", target="src/canary/foo.py"):
    return Proposal(pid, target, "bump", "r")


DIFF_A = """diff --git a/src/canary/foo.py b/src/canary/foo.py
--- a/src/canary/foo.py
+++ b/src/canary/foo.py
@@ -1 +1 @@
-X = 1
+X = 2
"""

DIFF_B = """diff --git a/src/canary/foo.py b/src/canary/foo.py
--- a/src/canary/foo.py
+++ b/src/canary/foo.py
@@ -1 +1 @@
-X = 2
+X = 3
"""

DIFF_ADD = """diff --git a/src/canary/newmod.py b/src/canary/newmod.py
new file mode 100644
--- /dev/null
+++ b/src/canary/newmod.py
@@ -0,0 +1 @@
+Y = 1
"""

DIFF_FLAG = """diff --git a/src/canary/foo.py b/src/canary/foo.py
--- a/src/canary/foo.py
+++ b/src/canary/foo.py
@@ -1 +1 @@
-X = 1
+X = 2
diff --git a/src/canary/EVAL_OK b/src/canary/EVAL_OK
new file mode 100644
--- /dev/null
+++ b/src/canary/EVAL_OK
@@ -0,0 +1 @@
+PASS
"""


def store_for(repo: str) -> Store:
    store = Store(repo / "runs" / "promotions")
    store.init_from_worktree(repo)
    return store


# --- sequences ---


def test_accepted_a_then_failing_b_same_file(repo):
    oc_a = revmod.apply_one(repo, prop("pA"), DIFF_A, ["true"], Journal())
    assert oc_a.kept
    oc_b = revmod.apply_one(repo, prop("pB"), DIFF_B, ["false"], Journal())
    assert not oc_b.kept
    assert (repo / "src/canary/foo.py").read_text() == "X = 2\n"
    store = Store(repo / "runs" / "promotions")
    manifest = store.rev_manifest(store.latest_rev())
    assert manifest["files"]["src/canary/foo.py"] != "absent"
    ok, _ = promotemod.worktree_matches(store, repo)
    assert ok


def test_failing_b_that_adds_files_leaves_no_trace(repo):
    oc = revmod.apply_one(repo, prop("pB"), DIFF_ADD, ["false"], Journal())
    assert not oc.kept
    assert not (repo / "src/canary/newmod.py").exists()
    store = Store(repo / "runs" / "promotions")
    manifest = store.rev_manifest(store.latest_rev())
    assert "src/canary/newmod.py" not in manifest["files"]


def test_second_round_starts_from_accepted_without_deadlock(repo):
    class DiffMuse:
        model = "m"

        def __init__(self, diffs):
            self.diffs = list(diffs)

        def complete(self, system, user, max_tokens=8000):
            return self.diffs.pop(0)

    doc1 = AssessmentDoc("R1", (prop("p1"),))
    rep1 = revmod.revise_round(repo, doc1, DiffMuse([DIFF_A]), Journal(), ["true"])
    assert rep1.kept == 1
    doc2 = AssessmentDoc("R2", (prop("p2"),))
    rep2 = revmod.revise_round(repo, doc2, DiffMuse([DIFF_B]), Journal(), ["true"])
    assert rep2.kept == 1  # no dirty-tree deadlock: gate compares to accepted
    assert (repo / "src/canary/foo.py").read_text() == "X = 3\n"


def test_run_outputs_do_not_veto_later_rounds(repo, tmp_path):
    """The run's own out/ writes are records, not code drift (demo52b)."""
    adir = repo / "out" / "assessments"

    class DiffMuse:
        model = "m"

        def __init__(self, diffs):
            self.diffs = list(diffs)

        def complete(self, system, user, max_tokens=8000):
            return self.diffs.pop(0)

    journal = Journal()
    rep1 = revmod.revise_round(repo, AssessmentDoc("R1", (prop("p1"),)),
                               DiffMuse([DIFF_A]), journal, ["true"],
                               assess_dir=adir)
    assert rep1.kept == 1
    # Mid-run output lands after the anchor, as a live run's would.
    (repo / "out" / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    (repo / "out" / "checkpoints").mkdir(parents=True, exist_ok=True)
    (repo / "out" / "checkpoints" / "c.json").write_text("{}\n", encoding="utf-8")
    rep2 = revmod.revise_round(repo, AssessmentDoc("R2", (prop("p2"),)),
                               DiffMuse([DIFF_B]), journal, ["true"],
                               assess_dir=adir)
    assert rep2.kept == 1
    assert (repo / "src/canary/foo.py").read_text() == "X = 3\n"
    assert (repo / "out" / "journal.jsonl").exists()  # records survive the sync
    assert not [n for n in journal.notes if n.event == "aborted"]


def test_record_ignore_never_excludes_code(repo, tmp_path):
    assert revmod._record_ignore(repo, None) == ()
    assert revmod._record_ignore(repo, tmp_path / "elsewhere") == ()
    assert revmod._record_ignore(repo, repo / "out" / "assessments") == ("out",)
    assert revmod._record_ignore(repo, repo / "src" / "canary") == ()
    assert revmod._record_ignore(repo, repo) == ()


def test_snapshot_ignore_is_symmetric(repo):
    (repo / "out").mkdir()
    (repo / "out" / "j.jsonl").write_text("{}\n", encoding="utf-8")
    store = Store(repo / "runs" / "promotions")
    store.init_from_worktree(repo, ("out",))
    (repo / "out" / "c.json").write_text("{}\n", encoding="utf-8")
    ok, _ = promotemod.worktree_matches(store, repo, ("out",))
    assert ok
    ok, divergent = promotemod.worktree_matches(store, repo)
    assert not ok and divergent  # without the ignore, the drift still shows


def test_eval_tree_is_fresh_git_repo(repo):
    """Git-shaped checks behave in the eval copy as at baseline (demo52c)."""
    store = Store(repo / "runs" / "promotions")
    store.init_from_worktree(repo)
    cand = promotemod.evaluate(store, prop("p1"), DIFF_A,
                               ["git", "rev-parse", "HEAD"], "a1")
    assert cand.test.get("exit") == 0


def test_evaluate_defaults_to_shared_check_cmd_and_persists_log(repo, tmp_path):
    store = Store(repo / "runs" / "promotions")
    store.init_from_worktree(repo)
    cand = promotemod.evaluate(store, prop("p1"), DIFF_A, None, "a1")
    assert cand.test["cmd"] == list(promotemod.CHECK_CMD) == ["pytest", "-q"]
    assert cand.test["log"].endswith(".log")
    import os

    assert os.path.exists(cand.test["log"])


GARBAGE_DIFF = "this is not a diff"
NAPPLY_DIFF = ("--- a/src/canary/foo.py\n+++ b/src/canary/foo.py\n"
               "@@ -1 +1 @@\n-X = 999\n+X = 2\n")


class RetryMuse:
    model = "m"

    def __init__(self, diffs):
        self.diffs = list(diffs)
        self.users: list[str] = []

    def complete(self, system, user, max_tokens=8000):
        self.users.append(user)
        return self.diffs.pop(0)


def test_malformed_diff_retried_with_feedback(repo):
    journal = Journal()
    rep = revmod.revise_round(repo, AssessmentDoc("R", (prop("p1"),)),
                              RetryMuse([GARBAGE_DIFF, DIFF_A]), journal, ["true"])
    assert rep.kept == 1
    assert (repo / "src/canary/foo.py").read_text() == "X = 2\n"
    assert any(n.event == "diff-retry" for n in journal.notes)


def test_retry_feedback_reaches_second_prompt(repo):
    muse = RetryMuse([GARBAGE_DIFF, DIFF_A])
    revmod.revise_round(repo, AssessmentDoc("R", (prop("p1"),)),
                        muse, Journal(), ["true"])
    assert len(muse.users) == 2
    assert "Previous attempt failed" in muse.users[1]
    assert "Previous attempt failed" not in muse.users[0]


def test_unapplicable_diff_skips_after_bounded_retry(repo):
    journal = Journal()
    muse = RetryMuse([GARBAGE_DIFF, NAPPLY_DIFF])
    rep = revmod.revise_round(repo, AssessmentDoc("R", (prop("p1"),)),
                              muse, journal, ["true"])
    assert rep.kept == 0 and rep.skipped == 1
    assert len(muse.users) == 2  # exactly one retry, then a recorded skip
    assert "no applicable diff after 2 attempts" in rep.outcomes[0].reason
    assert (repo / "src/canary/foo.py").read_text() == "X = 1\n"


def test_failed_diff_persists_candidate_record(repo, tmp_path):
    import json

    adir = tmp_path / "assess"
    revmod.revise_round(repo, AssessmentDoc("R", (prop("p1"),)),
                        RetryMuse([GARBAGE_DIFF, GARBAGE_DIFF]), Journal(),
                        ["true"], assess_dir=adir)
    saved = json.loads((adir / "candidates" / "p1.json").read_text(encoding="utf-8"))
    assert saved["status"] == "diff-failed"
    assert "no applicable diff" in saved["detail"]


def test_round_record_roundtrips_for_publish(repo):
    doc = AssessmentDoc("R1", (prop("p1"),))
    store = Store(repo / "runs" / "promotions")
    store.init_from_worktree(repo)

    class DiffMuse:
        model = "m"

        def complete(self, system, user, max_tokens=8000):
            return DIFF_A

    revmod.revise_round(repo, doc, DiffMuse(), Journal(), ["true"])
    assert (store.root / "rounds" / "latest.json").exists()
    doc2, rep2 = revmod.load_round(store)
    assert doc2.markdown == "R1" and rep2.kept == 1
    assert rep2.outcomes[0].target == "src/canary/foo.py"


# --- crashes ---


@pytest.mark.parametrize("point", ["validated", "applied", "tested", "promoting", "swapped"])
def test_crash_at_every_transition_recovers(repo, point):
    journal = Journal()
    store = store_for(repo)
    accepted_before = store.latest_rev()
    try:
        cand = promotemod.evaluate(store, prop(), DIFF_A, ["true"], "a1", journal,
                                   crash_at=frozenset({point}))
        promotemod.promote(store, cand, journal, crash_at=frozenset({point}))
        crashed = False
    except _CrashSim:
        crashed = True
    assert crashed
    summary = promotemod.recover(store, journal)
    cand = store.all_candidates()[0]
    if point in ("tested", "promoting", "swapped"):
        assert cand.state == "accepted"  # recorded pass completes
        assert store.latest_rev() != accepted_before
        assert summary["completed"]
    else:
        assert cand.state == "interrupted"
        assert store.latest_rev() == accepted_before
        assert summary["interrupted"] == [cand.id]
    ok, _ = promotemod.worktree_matches(store, repo)
    assert ok or point in ("tested", "promoting", "swapped")  # worktree sync is separate


def test_corrupt_accepted_falls_back_then_halts(repo):
    store = store_for(repo)
    first = store.latest_rev()
    cand = promotemod.evaluate(store, prop(), DIFF_A, ["true"], "a1", Journal())
    second = promotemod.promote(store, cand, Journal())
    (store.root / "accepted.json").write_text("{corrupt", encoding="utf-8")
    summary = promotemod.recover(store, Journal())
    assert summary["accepted"] == first  # predecessor restored
    (store.root / "accepted.json").write_text("{corrupt", encoding="utf-8")
    (store.root / "accepted.prev.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(PromotionHalt):
        promotemod.recover(store, Journal())
    assert store.rev_manifest(second) is not None  # artefacts never deleted


def test_concurrent_promotion_rejects_loser(repo):
    store = store_for(repo)
    a = promotemod.evaluate(store, prop("pA"), DIFF_A, ["true"], "a1", Journal())
    b = promotemod.evaluate(store, prop("pB"), DIFF_A, ["true"], "a1", Journal())
    promotemod.promote(store, a, Journal())
    with pytest.raises(PromotionError, match="moved"):
        promotemod.promote(store, b, Journal())
    assert store.load_candidate(b.id).state == "rejected"


def test_timeout_rejects_with_evidence(repo):
    store = store_for(repo)
    cand = promotemod.evaluate(store, prop(), DIFF_A, ["sleep", "30"], "a1", Journal(),
                               timeout_s=1)
    assert cand.state == "rejected" and "timed out" in cand.reason_detail
    assert cand.test["timeout"] is True


# --- tamper ---


def test_worker_success_flag_cannot_force_promotion(repo):
    store = store_for(repo)
    cand = promotemod.evaluate(store, prop(), DIFF_FLAG, ["false"], "a1", Journal())
    assert cand.state == "rejected"  # recorded exit 1 decides, not the flag file
    assert cand.test["exit"] == 1
    assert "EVAL_OK" not in (store.revs / store.latest_rev() / "manifest.json").read_text()


def test_rollback_restores_and_marks_superseded(repo):
    store = store_for(repo)
    first = store.latest_rev()
    a = promotemod.evaluate(store, prop("pA"), DIFF_A, ["true"], "a1", Journal())
    promotemod.promote(store, a, Journal())
    b = promotemod.evaluate(store, prop("pB"), DIFF_B, ["true"], "a1", Journal())
    promotemod.promote(store, b, Journal())
    rev_id = promotemod.rollback(store, first, "B was wrong", Journal(), ["true"])
    assert (store.revs / rev_id / "tree" / "src" / "canary" / "foo.py").read_text() == "X = 1\n"
    assert store.load_candidate(b.id).state == "rolled_back"
    assert store.load_candidate(a.id).state == "accepted"
    assert store.rev_manifest(rev_id)["restores"] == first


# --- credential split ---


def test_worker_refuses_maintainer_credentials(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    with pytest.raises(revmod.ContainmentBlocked, match="GITHUB_TOKEN must not enter"):
        revmod.revise_from_journal("notes", "ok", repo, None, rounds=1)
    with pytest.raises(revmod.ContainmentBlocked, match="GITHUB_TOKEN must not enter"):
        from canary import cycle as cyclemod

        cyclemod.run_cycle("q?", None, None, 1, 1, None, None, maintenance=True,
                           repo_root=str(repo))


def test_publish_is_separate_and_loads_recorded_round(repo, tmp_path, monkeypatch):
    from canary import cli

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    journal = Journal(run_dir / "journal.jsonl", run_id="r1")
    journal.note("cycle", "done", "seed finished")

    class DiffMuse:
        model = "m"

        def complete(self, system, user, max_tokens=8000):
            return DIFF_A

    revmod.revise_round(repo, AssessmentDoc("R1", (prop("p1"),)), DiffMuse(),
                        Journal(), ["true"])

    published = {}

    class FakeGH:
        def __init__(self, token, repo_name):
            published["token"] = token

        def create_issue(self, title, body):
            from collections import namedtuple

            published["issue"] = title
            return namedtuple("Issue", "number url")(7, "http://issue/7")

        def create_pr(self, title, body, head, base="main"):
            from collections import namedtuple

            return namedtuple("Pull", "url node_id number")(f"http://pr/{head}", "n", 9)

        def wait_and_merge(self, pr, timeout_s=0, interval_s=0):
            published["merged"] = True
            return True

        def comment(self, number, body):
            published["comment"] = (number, body)

        def delete_branch(self, branch):
            published["deleted"] = branch

    import canary.github_ops as ghmod

    monkeypatch.setattr(ghmod, "GitHub", FakeGH)
    monkeypatch.setattr(ghmod, "commit_and_push", lambda *a, **k: ["src/canary/foo.py"])
    monkeypatch.setattr("canary.cli.MuseClient", lambda *a, **k: None)
    monkeypatch.setenv("GITHUB_TOKEN", "maintainer-token")
    assert cli.main(["publish", "--repo", str(repo), "--run-dir", str(run_dir)]) == 0
    assert published["token"] == "maintainer-token"
    assert published["issue"].startswith("Maintenance ")


def test_publish_without_record_fails_cleanly(repo, tmp_path, capsys):
    from canary import cli

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    Journal(run_dir / "journal.jsonl", run_id="r1")
    assert cli.main(["publish", "--repo", str(repo), "--run-dir", str(run_dir)]) == 1
    assert "canary: error" in capsys.readouterr().err
