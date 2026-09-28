#!/usr/bin/env python3
"""Console tee: timestamped stdout/stderr capture for guest commands.

Runs the child with merged streams, prefixes every line with a UTC
timestamp, and writes it both to the log file and to our own stdout.
Exit code is the child's (128+sig when signaled). SIGTERM/SIGINT are
forwarded so container stops stay fast.

As container PID 1 it also reaps orphaned grandchildren: anything the
child's subtree detaches reparents to us, and unreaped zombies exhaust
the container's pids-limit until fork fails (keep1j: 231 zombies, pytest
SIGSEGV'd). The reaper never touches the main child's own exit status.

If the log file cannot be opened, degrades to a plain exec: observability
must never break the guest command it observes.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone

_child: subprocess.Popen | None = None
_done = threading.Event()


def _reap_orphans() -> int:
    """Reap dead children except the main one; return count reaped.

    waitpid(-1) would steal the main child's exit, so orphans are found
    by scanning /proc (Linux-only; a no-op anywhere else) and reaped by
    pid. Best-effort throughout: reaping must never break the tee.
    """
    me = os.getpid()
    main_pid = _child.pid if _child is not None else -1
    reaped = 0
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                _, rest = fh.read().split(") ", 1)
            fields = rest.split()
            if fields[0] != "Z" or int(fields[1]) != me or int(pid) == main_pid:
                continue
            os.waitpid(int(pid), os.WNOHANG)
            reaped += 1
        except (OSError, IndexError, ValueError):
            continue
    return reaped


def _reaper() -> None:
    while not _done.wait(2.0):
        _reap_orphans()


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
    _done.clear()
    sweeper = threading.Thread(target=_reaper, daemon=True)
    sweeper.start()
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
    _done.set()
    sweeper.join(timeout=5)
    _reap_orphans()  # final sweep after the main child is reaped
    return 128 - rc if rc < 0 else rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
