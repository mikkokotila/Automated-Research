"""Build 03: offline launcher support (fixture wheelhouse, no network)."""
import shutil
import subprocess
import sys

import pytest

from scripts.make_fixture_wheel import WHEEL_NAME, build_wheel


def _installer():
    """Guest-identical installer when present, else the uv equivalent."""
    probe = subprocess.run([sys.executable, "-m", "pip", "--version"],
                           capture_output=True, timeout=60)
    if probe.returncode == 0:
        return [sys.executable, "-m", "pip"]
    if shutil.which("uv"):
        return ["uv", "pip"]
    pytest.skip("no offline wheel installer available")


def test_fixture_wheel_builds_and_installs_offline(tmp_path):
    whl = build_wheel(tmp_path / "wheels")
    assert whl.name == WHEEL_NAME
    target = tmp_path / "site"
    r = subprocess.run(
        [*_installer(), "install", "--quiet", "--no-index",
         "--no-deps", "--target", str(target), "--find-links", str(whl.parent),
         "fixture_pkg"],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-500:]
    probe = subprocess.run(
        [sys.executable, "-c", "import fixture_pkg; print(fixture_pkg.answer())"],
        capture_output=True, text=True, timeout=60, env={"PYTHONPATH": str(target), "PATH": "/usr/bin:/bin"},
    )
    assert probe.returncode == 0 and probe.stdout.strip() == "42"
