"""Runs dashboard daemon: serve the runs table UI and own run processes.

Localhost-only control plane (default 127.0.0.1:8789). Owns container runs
it starts (spawn/wait/register), pauses and unpauses live worker
containers, relaunches recorded specs, and serves the dashboard UI plus a
JSON API. No auth: never bound beyond loopback, owner machine only.

Runs survive daemon exit by design (detached process group); on every
listing the daemon reconciles registry rows against live containers, so a
restart never leaves phantom "running" rows.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import runs

DEFAULT_PORT = 8789
MAX_BODY = 64 * 1024
FINDINGS_TTL_S = 60
INSPECT_TTL_S = 2

UI_PATH = runs.ROOT / "scripts" / "dashboard" / "index.html"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "run")[:40]


def validate_launch(argv: list) -> list[str]:
    """Only the disposable-worker launcher may be started (contained by design).

    Anything else is refused: the daemon never runs arbitrary host commands.
    Tests monkeypatch this to substitute a stub launcher.
    """
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty string list")
    if len(argv) < 2 or argv[0] not in ("bash", "sh"):
        raise ValueError("launch must invoke scripts/container_run.sh via bash/sh")
    if not argv[1].replace("\\", "/").endswith("scripts/container_run.sh"):
        raise ValueError("launch must invoke scripts/container_run.sh")
    return list(argv)


def _docker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


class Supervisor:
    """Owns daemon-started runs; reconciles registry against live containers."""

    def __init__(self, registry: str | Path = runs.REGISTRY,
                 root: str | Path = runs.ROOT):
        self.registry = Path(registry)
        self.root = Path(root)
        self.owned: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self._inspect_cache: dict[str, tuple[float, dict]] = {}
        self.findings_cache: dict = {"at": 0.0, "data": None}

    # -- spawning --

    def start(self, name: str, brief: str, argv: list[str],
              env: dict | None = None) -> dict:
        argv = validate_launch(argv)
        extra = runs.sanitize_launch_env(env)
        if not name.strip() or len(name) > 120:
            raise ValueError("name must be 1..120 chars")
        if len(brief) > 2000:
            raise ValueError("brief must be at most 2000 chars")
        stamp = runs.utcnow()[:16].replace(":", "").replace("T", "-").replace("-", "")
        base = f"{slugify(name)}-{stamp}"
        key, bundle, n = base, None, 0
        while True:
            candidate = base if n == 0 else f"{base}-{n}"
            out = self.root / "container-out" / candidate
            if not out.exists() and runs.get(candidate, self.registry) is None:
                key, bundle = candidate, out
                break
            n += 1
        kind = argv[2] if len(argv) > 2 else "?"
        env = dict(os.environ, OUT=str(bundle), RUN_KEY=key,
                   RUN_NAME=name, RUN_BRIEF=brief,
                   CANARY_RUNS_REGISTRY=str(self.registry))
        env.update(extra)
        launcher_log = self.root / "container-out" / f"{key}.launcher.log"
        launcher_log.parent.mkdir(parents=True, exist_ok=True)
        logfh = open(launcher_log, "a", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(argv, cwd=self.root, env=env,
                                    stdout=logfh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        except Exception:
            logfh.close()
            raise
        with self.lock:
            self.owned[key] = proc
        runs.register_start(key, name, brief, kind, str(bundle), "",
                            shlex.join(argv), path=self.registry,
                            launch_argv=list(argv), launch_env=extra)
        threading.Thread(target=self._wait, args=(key, proc, logfh),
                         daemon=True).start()
        return {"key": key, "bundle": str(bundle)}

    def _wait(self, key: str, proc: subprocess.Popen, logfh) -> None:
        try:
            rc = proc.wait()
        finally:
            try:
                logfh.close()
            except OSError:
                pass
        # Finish and drop ownership atomically: reconcile interrupts any
        # unowned running row, so the two must never be observed apart.
        row = runs.get(key, self.registry)
        bundle = row.get("bundle", "") if row else ""
        with self.lock:
            try:
                runs.register_finish(key, "done" if rc == 0 else "failed",
                                     bundle, path=self.registry)
            finally:
                self.owned.pop(key, None)

    # -- live state --

    def inspect(self, container: str) -> dict:
        """Cached docker state probe. Missing docker/daemon → {live: False}."""
        now = time.monotonic()
        hit = self._inspect_cache.get(container)
        if hit and now - hit[0] < INSPECT_TTL_S:
            return hit[1]
        state: dict = {"live": False, "container_state": "missing"}
        try:
            r = _docker("inspect", "-f", "{{.State.Running}} {{.State.Paused}}",
                        container, timeout=10)
            if r.returncode == 0:
                running, paused = r.stdout.strip().split()
                live = running == "true"
                state = {"live": live,
                         "container_state": ("paused" if paused == "true"
                                             else "running" if live else "exited")}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        self._inspect_cache[container] = (now, state)
        return state

    def reconcile(self) -> list[dict]:
        """Fold live truth over registry rows; finish phantom running rows."""
        rows = runs.load(self.registry)
        for row in rows:
            container = row.get("container") or ""
            if row.get("status") in ("running", "paused") and container:
                row["live"] = self.inspect(container)["live"]
                row["container_state"] = self.inspect(container)["container_state"]
            else:
                row["live"] = False
            with self.lock:
                owned = row.get("key") in self.owned
                stale = row.get("status") in ("running", "paused")
                fresh = (runs.get(row["key"], self.registry)
                         if stale and not row["live"] and not owned else None)
                if (fresh is not None
                        and fresh.get("status") in ("running", "paused")):
                    finished = runs.register_finish(
                        row["key"], "interrupted", row.get("bundle", ""),
                        path=self.registry)
                else:
                    finished = None
                if finished:
                    row.update(finished)
                    row["live"] = False
        return rows

    def pause(self, key: str) -> dict:
        row = runs.get(key, self.registry)
        if row is None:
            raise KeyError(key)
        container = row.get("container") or ""
        if not container:
            raise RuntimeError("run has no container yet (launcher not started)")
        state = self.inspect(container)
        if not state["live"]:
            raise RuntimeError("container is not live")
        if state["container_state"] == "paused":
            return {"key": key, "status": "paused"}
        r = _docker("pause", container)
        if r.returncode != 0:
            raise RuntimeError(f"docker pause failed: {r.stderr.strip()[:200]}")
        self._inspect_cache.pop(container, None)
        row["status"] = "paused"
        runs.upsert(row, self.registry)
        return {"key": key, "status": "paused"}

    def unpause(self, key: str) -> dict:
        row = runs.get(key, self.registry)
        if row is None:
            raise KeyError(key)
        container = row.get("container") or ""
        if not container:
            raise RuntimeError("run has no container yet (launcher not started)")
        state = self.inspect(container)
        if state["container_state"] != "paused":
            if state["live"]:
                row["status"] = "running"
                runs.upsert(row, self.registry)
                return {"key": key, "status": "running"}
            raise RuntimeError("container is not live")
        r = _docker("unpause", container)
        if r.returncode != 0:
            raise RuntimeError(f"docker unpause failed: {r.stderr.strip()[:200]}")
        self._inspect_cache.pop(container, None)
        row["status"] = "running"
        runs.upsert(row, self.registry)
        return {"key": key, "status": "running"}

    def rerun(self, key: str, name: str = "", brief: str = "") -> dict:
        row = runs.get(key, self.registry)
        if row is None:
            raise KeyError(key)
        argv = row.get("launch_argv") or []
        if not (isinstance(argv, list) and argv
                and all(isinstance(a, str) for a in argv)):
            launch = row.get("launch", "")
            if not launch:
                raise RuntimeError("run has no recorded launch spec")
            try:
                argv = shlex.split(launch)
            except ValueError as e:
                raise RuntimeError(
                    f"recorded launch is not parseable: {e}") from e
        new_name = name or row.get("name", key) + " (rerun)"
        new_brief = brief or (row.get("brief", "") + f"\nRerun of {key}.")
        started = self.start(new_name, new_brief, argv,
                             env=row.get("launch_env") or {})
        started["from"] = key
        return started


def merged_timeline(bundle: str | Path, tail: int = 0) -> list[dict]:
    """Journal + ops events, one time-ordered stream for the raw log view."""
    items = []
    for ev in runs.journal_events(bundle):
        items.append({"t": ev.get("ts", ""), "seq": ev.get("seq", 0),
                      "source": "journal", "phase": ev.get("phase", ""),
                      "event": ev.get("event", ""),
                      "detail": str(ev.get("detail", ""))[:500]})
    for ev in runs.ops_events(bundle):
        op = ev.get("op", "")
        kind = ev.get("kind", "")
        event = ev.get("event", "")
        text = f"op={op} kind={kind} event={event}".strip()
        items.append({"t": ev.get("at", ""), "seq": 0, "source": "ops",
                      "phase": kind or "ops", "event": event, "detail": text})
    items.sort(key=lambda i: (i["t"] or "9", i["seq"]))
    return items[-tail:] if tail > 0 else items


def acceptance_runs(root: str | Path) -> list[dict]:
    """Acceptance verdict sets (Build 17 runbook evidence), newest last."""
    out = []
    for manifest in sorted(Path(root, "acceptance").glob("*/manifest.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out.append({"date": manifest.parent.name,
                    "generated_at": data.get("generated_at", "?"),
                    "repo_rev": str(data.get("repo_rev", "?"))[:12],
                    "verdicts": [v for v in data.get("verdicts", [])
                                 if isinstance(v, dict)]})
    return out


def console_tail(bundle: str | Path, tail: int = 200) -> dict:
    path = Path(bundle) / "console.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"present": False, "lines": [],
                "note": "no console.log (captured for runs started after the tee landed)"}
    return {"present": True, "lines": lines[-tail:] if tail > 0 else lines,
            "total_lines": len(lines)}


class Handler(BaseHTTPRequestHandler):
    supervisor: Supervisor = None  # type: ignore
    server_version = "CanaryRuns/1"

    def log_message(self, fmt, *args):  # quieter than BaseHTTPRequestHandler
        pass

    def _send(self, code: int, payload, ctype="application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            elif parsed.path == "/api/runs":
                self._send(200, {"runs": self.supervisor.reconcile()})
            elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/log"):
                key = parsed.path[len("/api/runs/"):-len("/log")]
                self._serve_log(key, query)
            elif parsed.path.startswith("/api/runs/"):
                key = parsed.path[len("/api/runs/"):]
                row = next((r for r in self.supervisor.reconcile()
                            if r.get("key") == key), None)
                if row is None:
                    self._send(404, {"error": f"unknown run {key}"})
                else:
                    self._send(200, row)
            elif parsed.path == "/api/findings":
                self._send(200, self._findings())
            elif parsed.path == "/api/usage":
                self._send(200, {"usage": self._usage()})
            elif parsed.path == "/api/experiments":
                self._send(200, {"experiments": acceptance_runs(self.supervisor.root)})
            else:
                self._send(404, {"error": "not found"})
        except (OSError, ValueError) as e:
            self._send(500, {"error": str(e)[:300]})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_json()
        try:
            if parsed.path == "/api/runs":
                started = self.supervisor.start(
                    str(body.get("name", "")), str(body.get("brief", "")),
                    body.get("argv", []), env=body.get("env"))
                self._send(201, started)
            elif parsed.path.endswith("/pause") and parsed.path.startswith("/api/runs/"):
                key = parsed.path[len("/api/runs/"):-len("/pause")]
                self._send(200, self.supervisor.pause(key))
            elif parsed.path.endswith("/unpause") and parsed.path.startswith("/api/runs/"):
                key = parsed.path[len("/api/runs/"):-len("/unpause")]
                self._send(200, self.supervisor.unpause(key))
            elif parsed.path.endswith("/rerun") and parsed.path.startswith("/api/runs/"):
                key = parsed.path[len("/api/runs/"):-len("/rerun")]
                self._send(201, self.supervisor.rerun(
                    key, str(body.get("name", "")), str(body.get("brief", ""))))
            else:
                self._send(404, {"error": "not found"})
        except KeyError as e:
            self._send(404, {"error": f"unknown run {e}"})
        except (ValueError, RuntimeError) as e:
            self._send(400, {"error": str(e)[:300]})

    def _serve_log(self, key, query) -> None:
        row = runs.get(key, self.supervisor.registry)
        if row is None:
            self._send(404, {"error": f"unknown run {key}"})
            return
        try:
            tail = int(query.get("tail", ["500"])[0] or 500)
        except ValueError:
            tail = 500
        stream = query.get("stream", ["all"])[0]
        bundle = row.get("bundle", "")
        payload: dict = {"key": key, "live": False}
        if row.get("status") in ("running", "paused") and row.get("container"):
            payload["live"] = self.supervisor.inspect(row["container"])["live"]
        if stream in ("all", "timeline"):
            payload["timeline"] = merged_timeline(bundle, tail)
        if stream in ("all", "console"):
            payload["console"] = console_tail(bundle, tail)
        if stream == "all":
            launcher = Path(str(bundle) + ".launcher.log")
            try:
                lines = launcher.read_text(encoding="utf-8",
                                           errors="replace").splitlines()
                payload["launcher"] = {"present": True, "lines": lines[-tail:]}
            except OSError:
                payload["launcher"] = {"present": False, "lines": []}
        self._send(200, payload)

    def _findings(self) -> dict:
        now = time.monotonic()
        cache = self.supervisor.findings_cache
        if now - cache["at"] < FINDINGS_TTL_S and cache["data"] is not None:
            return cache["data"]
        data: dict = {"issues": [], "prs": [], "error": ""}
        try:
            root = str(self.supervisor.root)
            issues = subprocess.run(
                ["gh", "issue", "list", "--state", "open", "--limit", "50",
                 "--json", "number,title,labels,updatedAt"], cwd=root,
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            prs = subprocess.run(
                ["gh", "pr", "list", "--state", "open", "--limit", "50",
                 "--json", "number,title,headRefName,updatedAt"], cwd=root,
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            if issues.returncode == 0:
                data["issues"] = json.loads(issues.stdout or "[]")
            if prs.returncode == 0:
                data["prs"] = json.loads(prs.stdout or "[]")
            if issues.returncode != 0 or prs.returncode != 0:
                data["error"] = "gh query partially failed"
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            data["error"] = f"gh unavailable: {e}"[:200]
        self.supervisor.findings_cache = {"at": now, "data": data}
        return data

    def _usage(self) -> list[dict]:
        rows = []
        for row in runs.load(self.supervisor.registry):
            usage = row.get("usage", {}) or {}
            rows.append({"key": row.get("key"), "name": row.get("name"),
                         "status": row.get("status"),
                         "model_calls": usage.get("model_calls", 0),
                         "tokens": usage.get("tokens", 0)})
        return rows


def make_server(port: int = DEFAULT_PORT, registry: str | Path = runs.REGISTRY,
                root: str | Path = runs.ROOT) -> ThreadingHTTPServer:
    """Loopback-bound server (port 0 picks an ephemeral port for tests)."""
    handler = type("RunsHandler", (Handler,), {})
    handler.supervisor = Supervisor(registry, root)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def serve(port: int = DEFAULT_PORT, registry: str | Path = runs.REGISTRY,
          root: str | Path = runs.ROOT) -> None:
    """Bind loopback and serve forever. Runs outlive this process by design."""
    server = make_server(port, registry, root)
    print(f"runs dashboard serving at http://127.0.0.1:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
