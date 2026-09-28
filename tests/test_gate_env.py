"""Gate startup env precedence: explicit env wins, repo .env seeds, else fail closed.

Hermetic: the script under test is a copy in tmp_path (never the repo
checkout, whose real .env must not be read), and `docker` is a logging
stub on PATH. Fixture keys only; nothing here touches real services.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

STUB_DOCKER = """#!/bin/bash
echo "--- $@" >> "$FAKE_DOCKER_LOG"
case "$1 $2" in
  "container inspect") exit 1 ;;  # no pre-existing service
  "network inspect") echo "true" ;;  # internal-only network present
esac
if [ "$1" = "create" ]; then
  echo "MUSE_API_KEY_LEN=${#MUSE_API_KEY}" >> "$FAKE_DOCKER_LOG"
  echo "MUSE_API_KEY_VAL=$MUSE_API_KEY" >> "$FAKE_DOCKER_LOG"
fi
exit 0
"""


@pytest.fixture
def gate_stage(tmp_path):
    stage = tmp_path / "stage"
    (stage / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "gate_service.sh",
                stage / "scripts" / "gate_service.sh")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "docker"
    stub.write_text(STUB_DOCKER)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stage


def _run(stage, tmp_path, env_extra=None):
    env = {"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
           "FAKE_DOCKER_LOG": str(tmp_path / "docker.log")}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", str(stage / "scripts" / "gate_service.sh"), "start"],
                          capture_output=True, text=True, timeout=60,
                          env={k: v for k, v in env.items() if v is not None})


def _created_with(tmp_path):
    log = (tmp_path / "docker.log").read_text()
    assert "create --name canary-gate" in log
    return log


@pytest.mark.skipif(os.name != "posix", reason="bash entry point")
def test_explicit_env_wins_over_dotenv(gate_stage, tmp_path):
    (gate_stage / ".env").write_text("MUSE_API_KEY=file-key\n")
    proc = _run(gate_stage, tmp_path, {"MUSE_API_KEY": "explicit-key"})
    assert proc.returncode == 0, proc.stderr
    assert "MUSE_API_KEY_VAL=explicit-key" in _created_with(tmp_path)
    assert "file-key" not in proc.stdout + proc.stderr


@pytest.mark.skipif(os.name != "posix", reason="bash entry point")
def test_dotenv_seeds_empty_environment(gate_stage, tmp_path):
    (gate_stage / ".env").write_text("MUSE_API_KEY=file-key\n")
    proc = _run(gate_stage, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "MUSE_API_KEY_VAL=file-key" in _created_with(tmp_path)
    assert "file-key" not in proc.stdout + proc.stderr  # values never echoed


@pytest.mark.skipif(os.name != "posix", reason="bash entry point")
def test_start_fails_closed_without_env_or_dotenv(gate_stage, tmp_path):
    proc = _run(gate_stage, tmp_path)
    assert proc.returncode != 0
    assert "MUSE_API_KEY" in proc.stderr
    assert not (tmp_path / "docker.log").exists()  # guard fires before docker
