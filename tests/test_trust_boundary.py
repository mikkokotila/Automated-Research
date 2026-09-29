"""Build 02 gate plus the Issue #52 interim role check.

Every public revision entry point refuses before any model call, subprocess,
or filesystem mutation unless exactly one launcher/operator role marker is
present (CANARY_GUEST=1 for disposable workers, CANARY_PUBLISHER=1 for the
owner publish driver). All other caller-controlled evidence (/.dockerenv,
container-mode strings, lookalike flags) still refuses. The markers are
interim and forgeable; host-side attestation is deferred (Issues #62-#64).
See docs/TRUST_BOUNDARY.md.
"""
import subprocess

import pytest

from canary import cli as climod
from canary import cycle as cyclemod
from canary import github_ops as ghmod
from canary import revise as revmod
from canary.assess import AssessmentDoc, Proposal
from canary.journal import Journal
from canary.schedule import ScheduleError, Scope
from canary.spec import InvalidSpec, RunSpec
from tests.test_milestone4 import DIFF_FOO, ScriptedMuse, mock_http
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


@pytest.fixture()
def main_repo(tmp_path):
    r = tmp_path / "main-repo"
    (r / "src" / "canary").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "src" / "canary" / "foo.py").write_text("X = 1\n", encoding="utf-8")
    (r / "tests" / "test_x.py").write_text("assert True\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=r, capture_output=True, check=True)
    return r


def _checkout(repo, branch: str) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=repo,
                   capture_output=True, check=True)


def test_guest_marker_authorizes_revise_round_genuine(repo, monkeypatch):
    """The real gate (no monkeypatch) lets a marked worker keep a green patch."""
    monkeypatch.setenv("CANARY_GUEST", "1")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    muse = ScriptedMuse()
    muse.queues["diff"] = [DIFF_FOO]
    rep = revmod.revise_round(repo, _doc(), muse, Journal(), ["true"])
    assert rep.kept == 1
    assert (repo / "src" / "canary" / "foo.py").read_text(encoding="utf-8") == "X = 2\n"


def test_both_role_markers_refuse(repo, fake_muse, monkeypatch):
    monkeypatch.setenv("CANARY_GUEST", "1")
    monkeypatch.setenv("CANARY_PUBLISHER", "1")
    with pytest.raises(revmod.ContainmentBlocked, match="ambiguous"):
        revmod.revise_round(repo, _doc(), fake_muse, Journal(), ["true"])
    assert fake_muse.calls == []


@pytest.mark.parametrize("value", ["0", "yes", "true", "", "2"])
def test_wrong_marker_values_refuse(repo, fake_muse, monkeypatch, value):
    monkeypatch.setenv("CANARY_GUEST", value)
    with pytest.raises(revmod.ContainmentBlocked):
        revmod.require_revision_trust()
    assert fake_muse.calls == []


def test_publisher_marker_authorizes_publish_issue_only(main_repo, monkeypatch):
    """The real gate lets a marked publisher file the round issue (nothing kept)."""
    monkeypatch.setenv("CANARY_PUBLISHER", "1")
    api = FakeGitHubAPI()
    pub = revmod.publish_round(main_repo, "rid", _doc(), revmod.ReviseReport(),
                               Journal(), api.client())
    assert pub.issue_number > 0
    assert pub.pr_url == ""


def test_publish_requires_main_branch(main_repo, monkeypatch):
    monkeypatch.setenv("CANARY_PUBLISHER", "1")
    _checkout(main_repo, "dev")
    with pytest.raises(revmod.ContainmentBlocked, match="main branch"):
        revmod.publish_round(main_repo, "rid", _doc(), revmod.ReviseReport(),
                             Journal(), FakeGitHubAPI().client())
    with pytest.raises(revmod.ContainmentBlocked, match="main branch"):
        ghmod.commit_and_push(main_repo, "auto/x", "msg", "fixture-token")


def test_prod_profile_requires_main_branch(main_repo):
    revmod.require_prod_branch(main_repo, "prod")  # on main: passes
    revmod.require_prod_branch(main_repo, "dev")  # dev: passes anywhere
    _checkout(main_repo, "dev")
    revmod.require_prod_branch(main_repo, "dev")
    with pytest.raises(revmod.ContainmentBlocked, match="main branch"):
        revmod.require_prod_branch(main_repo, "prod")
    with pytest.raises(revmod.ContainmentBlocked, match="unknown run profile"):
        revmod.require_prod_branch(main_repo, "staging")


def _guest_repo(tmp_path, base_rev: str):
    r = tmp_path / "guest-repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "x.py").write_text("X = 1\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "guest"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-qm", f"canary guest base {base_rev}"]):
        subprocess.run(["git", *args], cwd=r, capture_output=True, check=True)
    return r


def test_prod_profile_guest_checks_pinned_rev_not_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("CANARY_GUEST", "1")
    monkeypatch.setenv("CANARY_BASE_REV", "abc123")
    repo = _guest_repo(tmp_path, "abc123")
    revmod.require_prod_branch(repo, "prod")  # branch `guest`, pinned rev matches
    revmod.require_prod_branch(repo, "dev")


def test_prod_profile_guest_refuses_without_launcher_rev(tmp_path, monkeypatch):
    monkeypatch.setenv("CANARY_GUEST", "1")
    monkeypatch.delenv("CANARY_BASE_REV", raising=False)
    repo = _guest_repo(tmp_path, "abc123")
    with pytest.raises(revmod.ContainmentBlocked, match="CANARY_BASE_REV"):
        revmod.require_prod_branch(repo, "prod")


def test_prod_profile_guest_refuses_rev_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("CANARY_GUEST", "1")
    monkeypatch.setenv("CANARY_BASE_REV", "pinned-rev")
    repo = _guest_repo(tmp_path, "other-rev")
    with pytest.raises(revmod.ContainmentBlocked, match="not staged from the pinned rev"):
        revmod.require_prod_branch(repo, "prod")


def test_spec_rejects_unknown_profile():
    with pytest.raises(InvalidSpec, match="profile"):
        RunSpec(question="q?", profile="staging")
    assert RunSpec(question="q?").profile == "dev"


def test_scope_freezes_profile():
    scope = Scope(True, "/r", None, 1, profile="prod")
    assert Scope.from_dict(scope.to_dict()) == scope
    legacy = {"maintenance": True, "repo_root": "/r", "check_cmd": None,
              "revise_rounds": 1, "bandit": None}
    assert Scope.from_dict(legacy).profile == "dev"
    bad = dict(legacy, profile="staging")
    with pytest.raises(ScheduleError, match="profile"):
        Scope.from_dict(bad)


def test_parse_check_cmd():
    assert climod.parse_check_cmd(None) is None
    assert climod.parse_check_cmd("  ") is None
    assert climod.parse_check_cmd("pytest -q -m 'not live'") == [
        "pytest", "-q", "-m", "not live"]
    with pytest.raises(InvalidSpec, match="check-cmd"):
        climod.parse_check_cmd("pytest 'unterminated")


def test_cli_revise_prod_off_main_refuses_before_any_work(tmp_path, main_repo,
                                                          monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text('{"ts":"t","phase":"cycle","event":"done"}\n',
                                           encoding="utf-8")
    _checkout(main_repo, "dev")
    monkeypatch.setenv("CANARY_GUEST", "1")  # trusted role, wrong branch
    rc = climod.main(["revise", "--run-dir", str(run_dir), "--repo", str(main_repo),
                      "--profile", "prod"])
    assert rc == 1
    assert "refusing" in capsys.readouterr().err
    assert not (run_dir / "assessment.md").exists()
    assert _porcelain(main_repo) == ""
