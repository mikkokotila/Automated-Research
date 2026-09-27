"""Build 16: interleaved research-assessment-revision cycle."""

import json
import subprocess
import sys

import httpx
import pytest

from canary import cycle as cyclemod
from canary import report as reportmod
from canary import revise as revmod
from canary.cycle import ResumeError
from canary.journal import Journal
from canary.lifecycle import Lifecycle
from canary.muse_client import RequestBlocked
from canary.schedule import (ScheduleError, Scheduler, SchedulerConfig, Scope,
                             decide, supervise)
from canary.spec import Cancelled, RunBudget, RunSpec, StopReason
from tests.test_milestone4 import ScriptedMuse, grounded_review, mock_http


@pytest.fixture(autouse=True)
def _revision_trust(monkeypatch):
    """Explicit in-process seam: exercise revision logic with the gate open."""
    monkeypatch.setattr(revmod, "require_revision_trust", lambda: None)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src" / "canary").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "src" / "canary" / "foo.py").write_text("X = 1\n", encoding="utf-8")
    (r / "tests" / "test_x.py").write_text("assert True\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=r, capture_output=True, check=True)
    return r


DIFF_GOOD = """--- a/src/canary/foo.py
+++ b/src/canary/foo.py
@@ -1 +1 @@
-X = 1
+X = 2
"""

DIFF_BAD = """--- a/src/canary/foo.py
+++ b/src/canary/foo.py
@@ -1 +1 @@
-X = 1
+X = BROKEN
"""


def check_cmd():
    return [sys.executable, "-c",
            "import pathlib; t = pathlib.Path('src/canary/foo.py').read_text(); "
            "assert 'X' in t and 'BROKEN' not in t"]


def assess_reply(*proposals):
    return json.dumps({"assessment": "R", "proposals": list(proposals)})


def prop(pid, target="src/canary/foo.py", change="edit", reason="r"):
    return {"id": pid, "target": target, "change": change, "reason": reason}


def begin(out, **kw):
    params = {"question": "seed?", "max_iterations": 3, "max_papers": 5}
    params.update(kw)
    spec = RunSpec(**params)
    run_id = reportmod.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    return spec, journal


# --- scheduler unit ---


def test_decide_research_assess_stop_transitions():
    sched = Scheduler(config=SchedulerConfig(allow_revision=True))
    assert decide(sched).action == "research"  # nothing done yet
    sched.note_iteration(True, 0)
    d = decide(sched)
    assert (d.action, "cadence" in d.reason) == ("assess", True)
    sched.note_revision(0, False, 1)
    sched.note_iteration(False, 2)
    sched.note_iteration(False, 3)
    d = decide(sched)
    assert (d.action, d.reason) == ("stop", StopReason.NO_PROGRESS.value)


def test_revision_veto_spend_failed_and_scope():
    s = Scheduler(config=SchedulerConfig(allow_revision=True, max_revision_calls=2,
                                         max_failed_revisions=1))
    assert s.revision_veto() is None
    s.note_revision(0, True, 1)
    assert "failed revision" in (s.revision_veto() or "")
    s.failed_revisions = 0
    s.note_revision(0, False, 1)
    assert "capped" in (s.revision_veto() or "")
    assert Scheduler().revision_veto() == "revision outside the authorized scope"


def test_note_revision_counts_only_attempted_rounds():
    s = Scheduler()
    s.note_revision(0, False, 3)  # assessed, nothing proposed: not a failure
    assert (s.failed_revisions, s.revision_calls, s.assess_rounds) == (0, 3, 1)
    s.note_revision(0, False, 2, assessed=False)  # errored pass: spend counts only
    assert (s.failed_revisions, s.revision_calls, s.assess_rounds) == (0, 5, 1)
    s.note_revision(1, True, 1)
    assert s.failed_revisions == 0  # a keep resets the streak


def test_scheduler_serialization_roundtrip_and_rejects_unknown():
    s = Scheduler(config=SchedulerConfig(allow_revision=True),
                  scope=Scope(True, "/r", ("true",), 2))
    s.note_iteration(False, 1)
    clone = Scheduler.from_dict(json.loads(json.dumps(s.to_dict())))
    assert clone.to_dict() == s.to_dict()
    with pytest.raises(ScheduleError, match="not 1"):
        Scheduler.from_dict({"version": 99, "scope": {}})
    with pytest.raises(ScheduleError, match="scope"):
        Scope.from_dict({})
    assert Scope.from_dict(Scope(False, None, None, 0).to_dict()) == Scope(False, None, None, 0)


def test_scheduler_holds_no_budget_or_model_handle():
    assert not (set(vars(Scheduler())) & {"budget", "client", "model", "completer"})


# --- distinct stop fixtures with complete partial bundles ---


def _bundle_files(out):
    return {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()}


def test_no_progress_fixture_stops_and_keeps_partial_bundle(tmp_path):
    out = tmp_path / "run"
    spec, journal = begin(out, max_iterations=5)
    muse = ScriptedMuse()
    muse.queues["review"] = ["U1 [1].", "U2 [1]."]  # marker-only: ungrounded
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]']
    muse.queues["final"] = ["Final."]
    res = cyclemod.run_cycle("seed?", None, None, 5, 5, muse, mock_http(),
                             journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                             record_dir=str(out))
    assert res.stopped == StopReason.NO_PROGRESS
    assert [i.question for i in res.iterations] == ["seed?", "q2?"]
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    files = _bundle_files(out)
    assert "iterations/iter1-review.md" in files and "iterations/iter2-review.md" in files
    assert any(f.startswith("checkpoints/checkpoint-") for f in files)
    assert any(n.event == "no-progress" for n in journal.notes)
    assert reportmod.read_bundle(out)["manifest"]["status"] == "no_progress"


def test_repeated_question_fixture_keeps_partial_bundle(tmp_path):
    out = tmp_path / "run"
    spec, journal = begin(out)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1")]
    muse.queues["follow"] = ['[{"question": "SEED? ", "kind": "review", "rationale": "dup"}]']
    muse.queues["final"] = ["Final."]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(),
                             journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                             record_dir=str(out))
    assert res.stopped == StopReason.REPEATED_QUESTION
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    assert (out / "iterations" / "iter1-review.md").is_file()
    assert any(n.event == "repeated-question" for n in journal.notes)


class FailOnCall:
    """Completer wrapper failing the Nth call with an injected error."""

    model = "failer"

    def __init__(self, inner, n, error):
        self.inner, self.n, self.error, self.calls = inner, n, error, 0

    def complete(self, system, user, max_tokens=8000):
        self.calls += 1
        if self.calls == self.n:
            raise self.error
        return self.inner.complete(system, user, max_tokens)


def test_provider_failure_fixture_keeps_partial_bundle(tmp_path):
    out = tmp_path / "run"
    spec, journal = begin(out)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]']
    muse.queues["final"] = ["Final."]
    failing = FailOnCall(muse, 3, RequestBlocked("broker refused"))
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, failing, mock_http(),
                             journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                             record_dir=str(out))
    assert res.stopped == StopReason.PROVIDER_BLOCKED
    assert [i.question for i in res.iterations] == ["seed?"]
    assert (out / "iterations" / "iter1-review.md").is_file()
    assert any(n.event == "provider-blocked" for n in journal.notes)


def test_cancel_fixture_keeps_partial_bundle(tmp_path):
    out = tmp_path / "run"
    spec, journal = begin(out)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]']
    muse.queues["final"] = ["Final."]
    failing = FailOnCall(muse, 3, Cancelled("operator stop"))
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, failing, mock_http(),
                             journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                             record_dir=str(out))
    assert res.stopped == StopReason.CANCELLED
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    assert reportmod.read_bundle(out)["manifest"]["status"] == "cancelled"
    assert (out / "iterations" / "iter1-review.md").is_file()


def test_budget_fixture_seals_partial_bundle(tmp_path):
    out = tmp_path / "run"
    spec, journal = begin(out, max_iterations=5, max_model_calls=3)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]']
    muse.queues["final"] = ["Final."]
    res = cyclemod.run_cycle("seed?", None, None, 5, 5, muse, mock_http(),
                             journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                             record_dir=str(out))
    assert res.stopped == StopReason.BUDGET_EXHAUSTED
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    assert reportmod.read_bundle(out)["manifest"]["status"] == "budget_exhausted"
    assert (out / "iterations" / "iter1-review.md").is_file()


# --- restart boundary ---


def test_mid_run_accept_halts_for_fresh_worker(tmp_path, repo):
    out = tmp_path / "run"
    spec, journal = begin(out, maintenance=True)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1")]
    muse.queues["assess"] = [assess_reply(prop("p1"))]
    muse.queues["diff"] = [DIFF_GOOD]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(), journal=journal,
                             maintenance=True, repo_root=str(repo), check_cmd=check_cmd(),
                             spec=spec, budget=RunBudget.from_spec(spec), record_dir=str(out))
    assert res.stopped == StopReason.RESTART_REQUIRED
    assert res.synthesis == ""  # no synthesis on a restart leg
    assert (repo / "src" / "canary" / "foo.py").read_text() == "X = 2\n"
    from canary import promote as promotemod

    latest = promotemod.Store(repo / "runs" / "promotions").latest_rev()
    assert reportmod.read_checkpoints(out)["restart_pending"] == latest
    assert any(n.event == "restart-required" for n in journal.notes)
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    assert reportmod.read_bundle(out)["manifest"]["status"] == "restart_required"


def test_restart_leg_continues_on_new_revision(tmp_path, repo):
    out = tmp_path / "run"
    spec, journal = begin(out, maintenance=True)
    leg1 = ScriptedMuse()
    leg1.queues["review"] = [grounded_review("R1")]
    leg1.queues["assess"] = [assess_reply(prop("p1"))]
    leg1.queues["diff"] = [DIFF_GOOD]
    res1 = cyclemod.run_cycle("seed?", None, None, 3, 5, leg1, mock_http(), journal=journal,
                              maintenance=True, repo_root=str(repo), check_cmd=check_cmd(),
                              spec=spec, budget=RunBudget.from_spec(spec), record_dir=str(out))
    reportmod.write_cycle_bundle(out, "seed?", res1, journal, spec)
    expect = reportmod.read_checkpoints(out)["restart_pending"]
    leg2 = ScriptedMuse()
    leg2.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]', "[]"]
    leg2.queues["review"] = [grounded_review("R2")]
    leg2.queues["final"] = ["Final."]
    res2, journal2, spec2, _ = cyclemod.resume_after_revision(
        out, leg2, mock_http(), expect_revision=expect)
    assert [i.question for i in res2.iterations] == ["seed?", "q2?"]
    assert res2.stopped == StopReason.CONVERGED
    assert res2.usage["model_calls"] == 7  # 3 before the restart, 4 after
    events = [n.event for n in journal2.notes]
    assert "re-anchored" in events and "scope-narrowed" in events
    assert "mid-revised" not in events[events.index("scope-narrowed"):]
    assert reportmod.read_checkpoints(out)["scope"]["maintenance"] is False
    assert (repo / "src" / "canary" / "foo.py").read_text() == "X = 2\n"


def test_plain_resume_refuses_restart_bundle(tmp_path, repo):
    out = tmp_path / "run"
    spec, journal = begin(out, maintenance=True)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1")]
    muse.queues["assess"] = [assess_reply(prop("p1"))]
    muse.queues["diff"] = [DIFF_GOOD]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(), journal=journal,
                             maintenance=True, repo_root=str(repo), check_cmd=check_cmd(),
                             spec=spec, budget=RunBudget.from_spec(spec), record_dir=str(out))
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    with pytest.raises(ResumeError, match="already restart_required"):
        cyclemod.resume_cycle(out, ScriptedMuse(), mock_http())


def test_restart_leg_verifies_expected_revision(tmp_path, repo, monkeypatch):
    out = tmp_path / "run"
    spec, journal = begin(out, maintenance=True)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1")]
    muse.queues["assess"] = [assess_reply(prop("p1"))]
    muse.queues["diff"] = [DIFF_GOOD]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(), journal=journal,
                             maintenance=True, repo_root=str(repo), check_cmd=check_cmd(),
                             spec=spec, budget=RunBudget.from_spec(spec), record_dir=str(out))
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    expect = reportmod.read_checkpoints(out)["restart_pending"]
    with pytest.raises(ResumeError, match="refusing to continue"):
        cyclemod.resume_after_revision(out, ScriptedMuse(), mock_http(),
                                       expect_revision="rev-nope")
    from canary import promote as promotemod

    monkeypatch.setattr(promotemod.Store, "latest_rev", lambda self: "rev-evil")
    with pytest.raises(ResumeError, match="unverified tree"):
        cyclemod.resume_after_revision(out, ScriptedMuse(), mock_http(),
                                       expect_revision=expect)


def test_restart_leg_refuses_finished_bundle(tmp_path):
    out = tmp_path / "run"
    spec, journal = begin(out)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1")]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(),
                             journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                             record_dir=str(out))
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    with pytest.raises(ResumeError, match="run status is converged"):
        cyclemod.resume_after_revision(out, ScriptedMuse(), mock_http(),
                                       expect_revision="rev-x")


def test_rejected_patch_leaves_research_on_previous_revision(tmp_path, repo):
    out = tmp_path / "run"
    spec, journal = begin(out, maintenance=True)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]', "[]"]
    muse.queues["final"] = ["Final."]
    muse.queues["assess"] = [assess_reply(prop("p1")), assess_reply(), assess_reply()]
    muse.queues["diff"] = [DIFF_BAD]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(), journal=journal,
                             maintenance=True, repo_root=str(repo), check_cmd=check_cmd(),
                             spec=spec, budget=RunBudget.from_spec(spec), record_dir=str(out))
    assert res.stopped == StopReason.CONVERGED  # research continued past the rejection
    assert [i.question for i in res.iterations] == ["seed?", "q2?"]
    assert (repo / "src" / "canary" / "foo.py").read_text() == "X = 1\n"
    from canary import promote as promotemod

    store = promotemod.Store(repo / "runs" / "promotions")
    assert store.latest_rev() is not None  # rev0 anchored, nothing promoted past it
    assert (out / "assessments" / "candidates" / "p1.json").is_file()
    events = [n.event for n in journal.notes]
    assert "mid-revised" in events and "revised" in events


def test_failed_revision_rounds_veto_further_revision(tmp_path, repo):
    out = tmp_path / "run"
    spec, journal = begin(out, maintenance=True)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R%d" % i) for i in (1, 2, 3)]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]',
                             '[{"question": "q3?", "kind": "review", "rationale": "r"}]', "[]"]
    muse.queues["final"] = ["Final."]
    muse.queues["assess"] = [assess_reply(prop("p1")), assess_reply(prop("p2"))]
    muse.queues["diff"] = [DIFF_BAD, DIFF_BAD]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(), journal=journal,
                             maintenance=True, repo_root=str(repo), check_cmd=check_cmd(),
                             spec=spec, budget=RunBudget.from_spec(spec), record_dir=str(out))
    assert res.stopped == StopReason.CONVERGED
    events = [n.event for n in journal.notes]
    assert events.count("mid-revised") == 2  # third pass vetoed, research continued
    assert "mid-revise-failed" not in events and "revise-failed" not in events
    assert "revise-vetoed" in events  # post-run revision vetoed as well
    assert any("revision vetoed" in n.detail for n in journal.notes
               if n.phase == "schedule")


# --- checkpoint compatibility ---


def _latest_checkpoint_path(out):
    return sorted((out / "checkpoints").glob("checkpoint-*.json"))[-1]


def test_v1_checkpoint_migrates_explicitly(tmp_path):
    out = tmp_path / "run"
    spec, journal = begin(out)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]', "[]"]
    muse.queues["final"] = ["Final."]
    cyclemod.run_cycle("seed?", None, None, 1, 5, muse, mock_http(),
                       journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                       record_dir=str(out))
    path = _latest_checkpoint_path(out)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["schema_version"] = 1
    del state["scheduler"]
    del state["scope"]
    path.write_text(json.dumps(state), encoding="utf-8")
    cont = ScriptedMuse()
    cont.queues["follow"] = ["[]"]
    cont.queues["final"] = ["Final."]
    res, journal2, _, _ = cyclemod.resume_cycle(out, cont, mock_http())
    assert res.stopped == StopReason.CONVERGED
    assert any(n.event == "checkpoint-migrated" for n in journal2.notes)


def test_unknown_checkpoint_version_halts(tmp_path):
    out = tmp_path / "run"
    begin(out)
    (out / "checkpoints").mkdir()
    (out / "checkpoints" / "checkpoint-0000.json").write_text(
        '{"schema_version": 999, "seq": 0}', encoding="utf-8")
    with pytest.raises(ResumeError, match="no checkpoint committed"):
        cyclemod.resume_cycle(out, ScriptedMuse(), mock_http())


def test_checkpoint_missing_keys_halts_naming_them(tmp_path):
    out = tmp_path / "run"
    spec, _ = begin(out)
    (out / "checkpoints").mkdir()
    (out / "checkpoints" / "checkpoint-0000.json").write_text(
        json.dumps({"schema_version": 2, "seq": 0, "spec": spec.to_dict()}),
        encoding="utf-8")
    with pytest.raises(ResumeError, match="missing.*pending"):
        cyclemod.resume_cycle(out, ScriptedMuse(), mock_http())
    reportmod.finalize_manifest(out, "restart_required")
    with pytest.raises(ResumeError, match="missing.*pending"):
        cyclemod.resume_after_revision(out, ScriptedMuse(), mock_http(),
                                       expect_revision="rev-x")


def test_checkpoint_carries_scheduler_scope_and_restart_slot(tmp_path):
    out = tmp_path / "run"
    spec, journal = begin(out)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1")]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(),
                       journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                       record_dir=str(out))
    state = reportmod.read_checkpoints(out)
    assert state["schema_version"] == 2
    assert state["scheduler"]["version"] == 1
    assert state["scope"]["maintenance"] is False
    assert state["restart_pending"] is None


def test_scope_tamper_halts_resume(tmp_path):
    out = tmp_path / "run"
    spec, journal = begin(out)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]']
    muse.queues["final"] = ["Final."]
    cyclemod.run_cycle("seed?", None, None, 1, 5, muse, mock_http(),
                       journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                       record_dir=str(out))
    path = _latest_checkpoint_path(out)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["scope"]["maintenance"] = True  # goal/approval scope is frozen
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ResumeError, match="scope mismatch"):
        cyclemod.resume_cycle(out, ScriptedMuse(), mock_http())


def test_fork_carries_lessons_and_rejected_history(tmp_path, repo):
    out = tmp_path / "run"
    spec, journal = begin(out, maintenance=True)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1")]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    muse.queues["assess"] = [assess_reply(prop("p1")), assess_reply()]
    muse.queues["diff"] = [DIFF_BAD]
    cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(), journal=journal,
                       maintenance=True, repo_root=str(repo), check_cmd=check_cmd(),
                       spec=spec, budget=RunBudget.from_spec(spec), record_dir=str(out))
    parent_files = {str(p.relative_to(out / "assessments"))
                    for p in (out / "assessments").rglob("*") if p.is_file()}
    assert "memory.jsonl" in parent_files
    assert "candidates/p1.json" in parent_files
    cont = ScriptedMuse()
    cont.queues["follow"] = ["[]"]
    cont.queues["final"] = ["Fork final."]
    cont.queues["assess"] = [assess_reply()]
    _, _, _, target = cyclemod.resume_cycle(out, cont, mock_http(),
                                            fork_dir=tmp_path / "fork")
    fork_files = {str(p.relative_to(tmp_path / "fork" / "assessments"))
                  for p in (tmp_path / "fork" / "assessments").rglob("*") if p.is_file()}
    assert parent_files <= fork_files
    memory = json.loads((tmp_path / "fork" / "assessments" / "memory.jsonl").read_text())
    assert memory and memory[0]["status"] == "rejected"


# --- lifecycle hooks ---


def test_lifecycle_assessment_disabled_is_noop(tmp_path):
    journal = Journal()
    journal.note("review", "done", "ok")
    assert Lifecycle(journal, tmp_path / "out").finish_assessment("done", ScriptedMuse()) is None
    assert not (tmp_path / "out" / "assessments").exists()


def test_lifecycle_assessment_persists_twins(tmp_path):
    journal = Journal()
    journal.note("review", "done", "ok")
    muse = ScriptedMuse()
    muse.queues["assess"] = [assess_reply(prop("p1"))]
    record = Lifecycle(journal, tmp_path / "out", assess_enabled=True).finish_assessment(
        "done", muse)
    assert record is not None and len(record.proposals) == 1
    assert (tmp_path / "out" / "assessments" / f"{record.id}.json").is_file()
    assert (tmp_path / "out" / "assessments" / f"{record.id}.md").is_file()
    assert any(n.event == "assessed" for n in journal.notes)


def test_lifecycle_malformed_assessment_never_fails_research(tmp_path):
    journal = Journal()
    journal.note("review", "done", "ok")
    muse = ScriptedMuse()
    muse.queues["assess"] = ["junk, no json"]
    assert Lifecycle(journal, tmp_path / "out", assess_enabled=True).finish_assessment(
        "done", muse) is None
    assert any(n.event == "assess-failed" for n in journal.notes)


def _offline_cli(monkeypatch, muse):
    """Route the CLI's model + literature calls to scripted fakes."""
    from canary import cli as climod

    def handler(req: httpx.Request) -> httpx.Response:
        if "openalex" in str(req.url):
            return httpx.Response(200, json={"results": [{
                "id": "W1", "title": "Study on X", "doi": "https://doi.org/10.1/x",
                "publication_year": 2023, "cited_by_count": 5,
                "authorships": [{"author": {"display_name": "A. Uthor"}}],
                "primary_location": {"source": {"display_name": "J X"}},
                "abstract_inverted_index": {"X": [0]}}]})
        return httpx.Response(200, json={"data": []})

    real_client = httpx.Client
    monkeypatch.setattr(climod, "MuseClient", lambda *a, **k: muse)
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    return climod


def test_review_assess_persists_assessment_and_default_does_not(tmp_path, monkeypatch):
    from canary.spec import RunSpec

    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R")]
    muse.queues["assess"] = [assess_reply()]
    climod = _offline_cli(monkeypatch, muse)
    out = tmp_path / "review"
    path = climod.review(RunSpec(question="seed?", max_papers=5, out_dir=str(out)), assess=True)
    assert path.endswith("review.md")
    twins = list((out / "assessments").glob("assess-*.json"))
    assert twins  # chunk record + merged record, each with a Markdown twin
    for twin in twins:
        assert (out / "assessments" / (twin.stem + ".md")).is_file()
        assert "proposals" in json.loads(twin.read_text(encoding="utf-8"))

    muse2 = ScriptedMuse()
    muse2.queues["review"] = [grounded_review("R")]
    _offline_cli(monkeypatch, muse2)
    out2 = tmp_path / "plain"
    climod.review(RunSpec(question="seed?", max_papers=5, out_dir=str(out2)))
    assert not (out2 / "assessments").exists()
    assert not list(out2.rglob("candidates"))


def test_cycle_assess_matches_standalone_artefacts(tmp_path, monkeypatch):
    from canary.spec import RunSpec

    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R")]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    muse.queues["assess"] = [assess_reply()]
    climod = _offline_cli(monkeypatch, muse)
    out = tmp_path / "cycle"
    spec = RunSpec(question="seed?", max_papers=5, out_dir=str(out))
    path = climod.cycle(spec, str(tmp_path), assess=True)
    assert path.endswith("synthesis.md")
    twins = list((out / "assessments").glob("assess-*.json"))
    assert twins
    for twin in twins:
        assert (out / "assessments" / (twin.stem + ".md")).is_file()
    assert reportmod.read_bundle(out)["manifest"]["status"] == "converged"
    assert not list(out.rglob("candidates"))  # assess never revises


def test_commands_never_revise_by_default(tmp_path, repo):
    journal = Journal()
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R")]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http(), journal=journal,
                             repo_root=str(repo))
    assert res.stopped == StopReason.CONVERGED
    assert not (repo / "runs").exists()
    events = [n.event for n in journal.notes]
    assert "mid-revised" not in events and "revised" not in events


def test_resume_flags_fork_and_expect_revision_are_exclusive(tmp_path):
    from canary import cli as climod

    args = type("Args", (), {"bundle": str(tmp_path), "fork": "f", "expect_revision": "r"})
    with pytest.raises(ResumeError, match="exclusive"):
        climod.resume_bundle(args)


# --- supervisor ---


def _restart_bundle(out, rev="rev-abc"):
    spec = RunSpec(question="seed?", max_iterations=3, max_papers=5)
    run_id = reportmod.begin_run(out, spec, "cycle")
    (out / "journal.jsonl").write_text("", encoding="utf-8")
    reportmod.write_checkpoint(out, {"seq": 0, "run_id": run_id, "spec": spec.to_dict(),
                                    "restart_pending": rev})
    reportmod.finalize_manifest(out, "restart_required")
    return spec


def test_supervise_drives_legs_until_sealed(tmp_path):
    out = tmp_path / "run"
    _restart_bundle(out)
    seen = []

    def fake_spawn(bundle, rev):
        seen.append((bundle, rev))
        reportmod.finalize_manifest(bundle, "converged")

    journal = Journal()
    assert supervise(out, journal, spawn=fake_spawn) == {"restarts": 1, "status": "converged"}
    assert seen == [(str(out), "rev-abc")]
    assert any(n.event == "restart" for n in journal.notes)


def test_supervise_refuses_to_loop(tmp_path):
    out = tmp_path / "run"
    _restart_bundle(out)
    with pytest.raises(ScheduleError, match="exhausted the supervisor cap"):
        supervise(out, Journal(), spawn=lambda b, r: None, max_restarts=1)


def test_supervise_noop_without_restart(tmp_path):
    out = tmp_path / "run"
    _restart_bundle(out)
    reportmod.finalize_manifest(out, "converged")
    called = []
    assert supervise(out, Journal(), spawn=lambda b, r: called.append(r)) == {
        "restarts": 0, "status": "converged"}
    assert called == []


def test_supervise_without_journal_appends_durably(tmp_path):
    out = tmp_path / "run"
    _restart_bundle(out)

    def fake_spawn(bundle, rev):
        reportmod.finalize_manifest(bundle, "converged")

    supervise(out, spawn=fake_spawn)
    text = (out / "journal.jsonl").read_text(encoding="utf-8")
    assert "restart" in text and "rev-abc" in text
