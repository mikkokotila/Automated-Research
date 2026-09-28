#!/usr/bin/env python3
"""Console tee: timestamped stdout/stderr capture for guest commands.

Runs the child with merged streams, prefixes every line with a UTC
timestamp, and writes it both to the log file and to our own stdout.
Exit code is the child's (128+sig when signaled). SIGTERM/SIGINT are
forwarded so container stops stay fast.

If the log file cannot be opened, degrades to a plain exec: observability
must never break the guest command it observes.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime, timezone

_child: subprocess.Popen | None = None


def _forward(signum, _frame) -> None:
    if _child is not None and _child.poll() is None:
        try:
            _child.send_signal(signum)
        except OSError:
            pass


def _stamp(line: str) -> str:
    return datetime.now(timezone.utc).isoformat() + " " + line


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(f"usage: {argv[0]} <logpath> <cmd...>", file=sys.stderr)
        return 2
    logpath, cmd = argv[1], argv[2:]
    try:
        parent = os.path.dirname(os.path.abspath(logpath))
        os.makedirs(parent, exist_ok=True)
        log = open(logpath, "a", encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"console_tee: cannot open {logpath}: {e}; running bare",
              file=sys.stderr, flush=True)
        os.execvp(cmd[0], cmd)
        return 127  # unreachable; exec replaces us
    global _child
    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)
    _child = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              text=True, errors="replace", bufsize=1)
    assert _child.stdout is not None
    try:
        with log:
            for line in _child.stdout:
                stamped = _stamp(line if line.endswith("\n") else line + "\n")
                log.write(stamped)
                log.flush()
                sys.stdout.write(stamped)
                sys.stdout.flush()
    except BrokenPipeError:
        pass
    rc = _child.wait()
    return 128 - rc if rc < 0 else rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
