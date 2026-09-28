"""Console tee: capture, exit codes, and signal forwarding."""
import os
import signal
import subprocess
import sys
from pathlib import Path

TEE = Path(__file__).resolve().parents[1] / "scripts" / "console_tee.py"


def run_tee(logpath, cmd, **kw):
    return subprocess.run([sys.executable, str(TEE), str(logpath), *cmd],
                          capture_output=True, text=True, timeout=60, **kw)


def test_exit_code_passthrough(tmp_path):
    assert run_tee(tmp_path / "c.log", ["true"]).returncode == 0
    assert run_tee(tmp_path / "c.log", ["false"]).returncode == 1
    proc = run_tee(tmp_path / "c.log", ["sh", "-c", "exit 3"])
    assert proc.returncode == 3


def test_lines_timestamped_and_mirrored(tmp_path):
    log = tmp_path / "c.log"
    proc = run_tee(log, ["sh", "-c", "echo out; echo err >&2"])
    assert proc.returncode == 0
    body = log.read_text(encoding="utf-8")
    assert " out\n" in body and " err\n" in body  # merged streams
    assert proc.stdout == body  # stdout mirrors the file
    first = body.splitlines()[0]
    assert first.startswith("20") and "T" in first  # ISO UTC stamp


def test_sigterm_forwarded_and_fast(tmp_path):
    log = tmp_path / "c.log"
    proc = subprocess.Popen([sys.executable, str(TEE), str(log), "sleep", "30"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=5)
        raise AssertionError("tee exited before the signal")
    except subprocess.TimeoutExpired:
        pass  # child still sleeping, as expected
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 128 + signal.SIGTERM


def test_unwritable_log_degrades_to_bare_exec(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    proc = run_tee(blocker / "c.log", ["sh", "-c", "echo hi; exit 4"])
    assert proc.returncode == 4  # child ran and its code passed through
    assert "hi\n" in proc.stdout
    assert "running bare" in proc.stderr


def test_usage_error():
    proc = subprocess.run([sys.executable, str(TEE)], capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 2
