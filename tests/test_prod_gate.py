"""Host-side prod gate (#64): prod pins HEAD to origin/main pre-launch."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "prod_gate.sh"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                   check=True)


def _clone(tmp_path):
    """Bare origin + fresh clone on main, one commit, pushed."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], capture_output=True,
                   check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], capture_output=True,
                   check=True)
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-qb", "main")
    (work / "f.txt").write_text("1\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "push", "-q", "origin", "main")
    return origin, work


def _run(repo, *args):
    return subprocess.run(["bash", str(GATE), str(repo), *args], capture_output=True,
                          text=True)


def test_dev_and_default_pass_anywhere(tmp_path):
    _, work = _clone(tmp_path)
    _git(work, "checkout", "-qb", "feature")
    assert _run(work, "cycle", "q?").returncode == 0
    assert _run(work, "cycle", "--profile", "dev", "q?").returncode == 0
    assert _run(work, "cycle", "--profile=dev", "q?").returncode == 0


def test_prod_passes_on_synced_main(tmp_path):
    _, work = _clone(tmp_path)
    assert _run(work, "cycle", "--profile", "prod", "q?").returncode == 0
    assert _run(work, "cycle", "--profile=prod", "q?").returncode == 0


def test_prod_refuses_stale_and_ahead_main(tmp_path):
    origin, work = _clone(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], capture_output=True,
                   check=True)
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    (other / "f.txt").write_text("2\n", encoding="utf-8")
    _git(other, "commit", "-qam", "ahead")
    _git(other, "push", "-q", "origin", "HEAD:main")
    r = _run(work, "cycle", "--profile", "prod", "q?")
    assert r.returncode == 2 and "!=" in r.stderr  # stale
    (work / "f.txt").write_text("3\n", encoding="utf-8")
    _git(work, "commit", "-qam", "local")
    r = _run(work, "cycle", "--profile", "prod", "q?")
    assert r.returncode == 2 and "!=" in r.stderr  # diverged


def test_prod_refuses_branch_and_detached(tmp_path):
    _, work = _clone(tmp_path)
    _git(work, "checkout", "-qb", "feature")
    r = _run(work, "cycle", "--profile", "prod", "q?")
    assert r.returncode == 2 and "main branch" in r.stderr
    _git(work, "checkout", "-q", "main")
    _git(work, "checkout", "-q", "--detach", "HEAD")
    r = _run(work, "cycle", "--profile", "prod", "q?")
    assert r.returncode == 2 and "main branch" in r.stderr


def test_prod_refuses_unverifiable_remote(tmp_path):
    _, work = _clone(tmp_path)
    _git(work, "remote", "remove", "origin")
    r = _run(work, "cycle", "--profile", "prod", "q?")
    assert r.returncode == 2 and "unverifiable" in r.stderr


def test_prod_refuses_unknown_profile_and_dangling_flag(tmp_path):
    _, work = _clone(tmp_path)
    r = _run(work, "cycle", "--profile", "banana", "q?")
    assert r.returncode == 2 and "unknown run profile" in r.stderr
    r = _run(work, "cycle", "--profile")
    assert r.returncode == 2 and "without a value" in r.stderr


def test_launcher_invokes_gate_before_build():
    text = (ROOT / "scripts" / "container_run.sh").read_text(encoding="utf-8")
    gate_at = text.index("prod_gate.sh")
    assert gate_at < text.index("docker build")  # gate runs before any build/run
    assert 'bash scripts/prod_gate.sh . "$@"' in text
