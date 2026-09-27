"""Build 02: uncontained revision fails closed.

Every public revision entry point refuses before any model call, subprocess,
or filesystem mutation. Caller-controlled evidence (environment flags,
/.dockerenv, container-mode strings) never authorizes revision: no trusted
execution path exists until Builds 03-04 land (see docs/TRUST_BOUNDARY.md).
"""
import subprocess

import pytest

from canary import cli as climod
from canary import cycle as cyclemod
from canary import github_ops as ghmod
from canary import revise as revmod
from canary.assess import AssessmentDoc, Proposal
from canary.journal import Journal
from tests.test_milestone4 import ScriptedMuse, mock_http
from tests.test_operation import FakeGitHubAPI


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


def _porcelain(repo) -> str:
    r = subprocess.run(["git", "status", "--porcelain", "--", "src", "tests"],
                       cwd=repo, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _doc() -> AssessmentDoc:
    return AssessmentDoc("R", (Proposal("p1", "src/canary/foo.py", "c", "r"),))


def test_revise_round_refuses_before_subprocess(repo, fake_muse):
    with pytest.raises(revmod.ContainmentBlocked):
        revmod.revise_round(repo, _doc(), fake_muse, Journal(), ["true"])
    assert fake_muse.calls == []
    assert _porcelain(repo) == ""


def test_revise_from_journal_refuses_before_model_call(repo, fake_muse):
    journal = Journal()
    with pytest.raises(revmod.ContainmentBlocked):
        revmod.revise_from_journal("notes", "ok", repo, fake_muse, rounds=1, journal=journal)
    assert fake_muse.calls == []
    assert journal.notes == []


def test_publish_round_refuses_before_writes(repo):
    with pytest.raises(revmod.ContainmentBlocked):
        revmod.publish_round(repo, "rid", _doc(), revmod.ReviseReport(), Journal(),
                             FakeGitHubAPI().client())
    assert not (repo / "runs").exists()


def test_commit_and_push_refuses_without_host_effects(repo):
    with pytest.raises(revmod.ContainmentBlocked):
        ghmod.commit_and_push(repo, "auto/x", "msg", "fixture-token")
    assert _porcelain(repo) == ""


def test_spoofed_container_evidence_does_not_authorize(repo, fake_muse, monkeypatch):
    """Env flags, /.dockerenv, and container-mode strings are not authorization."""
    from pathlib import Path

    monkeypatch.setenv("CANARY_SANDBOXED", "1")
    monkeypatch.setenv("CONTAINER", "1")
    monkeypatch.setenv("CANARY_CONTAINER_MODE", "sandbox")
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists",
                        lambda self: True if str(self) == "/.dockerenv" else real_exists(self))
    with pytest.raises(revmod.ContainmentBlocked):
        revmod.revise_round(repo, _doc(), fake_muse, Journal(), ["true"])
    with pytest.raises(revmod.ContainmentBlocked):
        revmod.revise_from_journal("notes", "ok", repo, fake_muse, rounds=1)
    with pytest.raises(revmod.ContainmentBlocked):
        revmod.publish_round(repo, "rid", _doc(), revmod.ReviseReport(), Journal(),
                             FakeGitHubAPI().client())
    with pytest.raises(revmod.ContainmentBlocked):
        ghmod.commit_and_push(repo, "auto/x", "msg", "fixture-token")
    assert fake_muse.calls == []
    assert _porcelain(repo) == ""


def test_cli_revise_refuses_uncontained(tmp_path, repo, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text('{"ts":"t","phase":"cycle","event":"done"}\n',
                                           encoding="utf-8")
    monkeypatch.setenv("CANARY_SANDBOXED", "1")  # bypass attempt via public CLI
    monkeypatch.setenv("CANARY_GATE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture-only")
    rc = climod.main(["revise", "--run-dir", str(run_dir), "--repo", str(repo)])
    assert rc == 1
    assert "refusing" in capsys.readouterr().err
    assert not (run_dir / "assessment.md").exists()
    assert _porcelain(repo) == ""


def test_cli_cycle_maintenance_refuses_before_any_work(tmp_path, repo, monkeypatch, capsys):
    out = tmp_path / "out"
    monkeypatch.setenv("CANARY_SANDBOXED", "1")  # bypass attempt via public CLI
    monkeypatch.setenv("CANARY_GATE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture-only")
    rc = climod.main(["cycle", "seed?", "--maintenance", "--repo", str(repo),
                      "--out", str(out), "--max-iterations", "1"])
    assert rc == 1
    assert "refusing" in capsys.readouterr().err
    assert not (out / "journal.jsonl").exists()


def test_run_cycle_maintenance_refuses_before_model_call(repo, fake_muse):
    journal = Journal()
    with pytest.raises(revmod.ContainmentBlocked):
        cyclemod.run_cycle("seed?", None, None, 1, 5, fake_muse, mock_http(),
                           journal=journal, maintenance=True, repo_root=str(repo))
    assert fake_muse.calls == []
    assert journal.notes == []


def test_maintenance_without_repo_still_disables_loudly():
    """Existing contract: no repo means maintenance is skipped with a note, not a crash."""
    muse = ScriptedMuse()
    muse.queues["review"] = ["R [1]."]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["F."]
    journal = Journal()
    res = cyclemod.run_cycle(
        "seed?", None, None, 2, 5, muse, mock_http(), journal=journal,
        maintenance=True, repo_root=None)
    assert res.stopped == "converged"
    assert "revise-disabled" in [n.event for n in journal.notes]
