"""Dashboard daemon: validation, supervision, API, and log merge."""
import json
import os
import shlex
import shutil
import subprocess
import threading
import time

import httpx
import pytest

from canary import dashboard, runs


def test_validate_launch_accepts_only_the_launcher():
    ok = ["bash", "scripts/container_run.sh", "cycle", "q?"]
    assert dashboard.validate_launch(ok) == ok
    for bad in ([], ["true"], ["bash"], ["bash", "/bin/true"],
                ["bash", "scripts/other.sh"], "bash scripts/container_run.sh",
                [None]):
        with pytest.raises(ValueError):
            dashboard.validate_launch(bad)


def test_slugify():
    assert dashboard.slugify("Hello, World!") == "hello-world"
    assert dashboard.slugify("!!!") == "run"
    assert len(dashboard.slugify("x" * 200)) == 40


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = tmp_path / "runs" / "registry.json"
    httpd = dashboard.make_server(0, registry=registry, root=tmp_path)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{httpd.server_port}",
                          timeout=10.0)
    yield {"client": client, "registry": registry, "root": tmp_path}
    client.close()
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _stub_launcher(root, rc=0, manifest_extra=None):
    """Fake launcher: writes a bundle like container_run.sh would, then exits."""
    stub = root / "stub-launch.sh"
    manifest = {"run_id": "stub-1", "kind": "cycle", "status": "converged",
                "started_at": "2026-09-28T08:00:00+00:00",
                "finished_at": "2026-09-28T08:01:00+00:00",
                "spec": {"question": "stub?", "profile": "dev"}}
    manifest.update(manifest_extra or {})
    run = {"model": "stub", "usage": {"model_calls": 2, "tokens": 50}}
    stub.write_text(
        "#!/bin/bash\n"
        f"mkdir -p \"$OUT\"\n"
        f"printf '%s' '{json.dumps(manifest)}' > \"$OUT/manifest.json\"\n"
        f"printf '%s' '{json.dumps(run)}' > \"$OUT/run.json\"\n"
        f"echo stub-console-line\n"
        f"exit {rc}\n")
    stub.chmod(0o755)
    return ["/bin/bash", str(stub)]


def _wait_for(client, key, want, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = client.get(f"/api/runs/{key}").json()
        if row.get("status") == want:
            return row
        time.sleep(0.2)
    raise AssertionError(f"{key} never became {want}: {row!r}")


def test_start_finishes_and_parses_bundle(server, monkeypatch):
    monkeypatch.setattr(dashboard, "validate_launch", lambda argv: list(argv))
    argv = _stub_launcher(server["root"])
    r = server["client"].post("/api/runs", json={
        "name": "stub run", "brief": "stub brief", "argv": argv})
    assert r.status_code == 201
    key = r.json()["key"]
    row = _wait_for(server["client"], key, "converged")
    assert row["name"] == "stub run" and row["run_id"] == "stub-1"
    assert row["usage"] == {"model_calls": 2, "tokens": 50}
    assert (server["root"] / "container-out" / f"{key}.launcher.log").exists()


def test_wait_finishes_before_dropping_ownership(server, monkeypatch):
    sup = dashboard.Supervisor(registry=server["registry"], root=server["root"])
    runs.register_start("k1", "n", "b", path=server["registry"])
    seen = {}
    real_finish = runs.register_finish

    def spy(key, *a, **k):
        # No lock: _wait already holds it across the finish by design.
        seen["owned"] = key in sup.owned
        return real_finish(key, *a, **k)

    monkeypatch.setattr(runs, "register_finish", spy)
    proc = subprocess.Popen(["true"])
    with sup.lock:
        sup.owned["k1"] = proc
    with open(os.devnull, "w") as logfh:
        sup._wait("k1", proc, logfh)
    assert seen["owned"] is True  # reconcile must see owned until finished
    with sup.lock:
        assert "k1" not in sup.owned


def test_failed_launch_marks_failed(server, monkeypatch):
    monkeypatch.setattr(dashboard, "validate_launch", lambda argv: list(argv))
    argv = _stub_launcher(server["root"], rc=1)
    r = server["client"].post("/api/runs", json={
        "name": "bad", "brief": "b", "argv": argv})
    row = _wait_for(server["client"], r.json()["key"], "failed")
    assert row["status"] == "failed"


def test_start_records_exact_argv(server, monkeypatch):
    monkeypatch.setattr(dashboard, "validate_launch", lambda argv: list(argv))
    argv = _stub_launcher(server["root"]) + ["seed with spaces?"]
    r = server["client"].post("/api/runs", json={
        "name": "argv probe", "brief": "b", "argv": argv})
    assert r.status_code == 201
    row = runs.get(r.json()["key"], server["registry"])
    assert row["launch_argv"] == argv
    assert row["launch"] == shlex.join(argv)


def test_rerun_prefers_recorded_argv(server, monkeypatch):
    monkeypatch.setattr(dashboard, "validate_launch", lambda argv: list(argv))
    argv = _stub_launcher(server["root"]) + ["seed with spaces?"]
    runs.register_start("orig", "n", "b", bundle="", container="",
                        launch="LOSSY STRING", path=server["registry"],
                        launch_argv=argv)
    r = server["client"].post("/api/runs/orig/rerun", json={"name": "re"})
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    assert key != "orig"
    row = _wait_for(server["client"], key, "converged")
    assert row["launch_argv"] == argv  # spaces survived; lossy string ignored


def test_start_applies_env_to_child_and_records_it(server, monkeypatch):
    monkeypatch.setattr(dashboard, "validate_launch", lambda argv: list(argv))
    stub = server["root"] / "stub-env.sh"
    manifest = {"run_id": "stub-1", "kind": "cycle", "status": "converged",
                "started_at": "2026-09-28T08:00:00+00:00",
                "finished_at": "2026-09-28T08:01:00+00:00",
                "spec": {"question": "stub?", "profile": "dev"}}
    run = {"model": "stub", "usage": {"model_calls": 2, "tokens": 50}}
    stub.write_text(
        "#!/bin/bash\n"
        'mkdir -p "$OUT"\n'
        'printf "%s" "${CANARY_STAGE:-unset}" > "$OUT/stage.txt"\n'
        f"printf '%s' '{json.dumps(manifest)}' > \"$OUT/manifest.json\"\n"
        f"printf '%s' '{json.dumps(run)}' > \"$OUT/run.json\"\n"
        "exit 0\n")
    stub.chmod(0o755)
    r = server["client"].post("/api/runs", json={
        "name": "env probe", "brief": "b",
        "argv": ["/bin/bash", str(stub)], "env": {"CANARY_STAGE": "dev"}})
    assert r.status_code == 201, r.text
    row = _wait_for(server["client"], r.json()["key"], "converged")
    assert row["launch_env"] == {"CANARY_STAGE": "dev"}
    stage = server["root"] / "container-out" / r.json()["key"] / "stage.txt"
    assert stage.read_text() == "dev"


def test_start_rejects_disallowed_env(server, monkeypatch):
    monkeypatch.setattr(dashboard, "validate_launch", lambda argv: list(argv))
    argv = _stub_launcher(server["root"])
    for env in ({"OUT": "x"}, {"PATH": "/bin"}, ["CANARY_STAGE"], {"CANARY_STAGE": 7}):
        r = server["client"].post("/api/runs", json={
            "name": "n", "brief": "b", "argv": argv, "env": env})
        assert r.status_code == 400, env


def test_rerun_carries_recorded_env(server, monkeypatch):
    monkeypatch.setattr(dashboard, "validate_launch", lambda argv: list(argv))
    argv = _stub_launcher(server["root"])
    runs.register_start("orig", "n", "b", bundle="", container="",
                        launch=shlex.join(argv), path=server["registry"],
                        launch_argv=argv, launch_env={"CANARY_STAGE": "dev"})
    r = server["client"].post("/api/runs/orig/rerun", json={"name": "re"})
    assert r.status_code == 201, r.text
    row = runs.get(r.json()["key"], server["registry"])
    assert row["launch_env"] == {"CANARY_STAGE": "dev"}
    _wait_for(server["client"], r.json()["key"], "converged")


def test_start_rejects_bad_input(server):
    c = server["client"]
    assert c.post("/api/runs", json={
        "name": "", "brief": "b",
        "argv": ["bash", "scripts/container_run.sh", "cycle"]}).status_code == 400
    assert c.post("/api/runs", json={
        "name": "n", "brief": "b", "argv": ["true"]}).status_code == 400
    assert c.post("/api/runs/nonexistent/pause").status_code == 404
    assert c.get("/api/runs/nonexistent").status_code == 404
    assert c.get("/api/nope").status_code == 404


def test_pause_without_container_refuses(server):
    runs.register_start("k1", "n", "b", path=server["registry"])
    r = server["client"].post("/api/runs/k1/pause")
    assert r.status_code == 400
    assert "no container" in r.json()["error"]


def test_reconcile_finishes_phantom_running(server):
    runs.register_start("k1", "n", "b", bundle="gone", container="canary-nope",
                        path=server["registry"])
    rows = server["client"].get("/api/runs").json()["runs"]
    assert rows[0]["status"] == "interrupted"
    assert rows[0]["live"] is False


def test_rerun_requires_recorded_launch(server):
    runs.register_start("k1", "n", "b", path=server["registry"])
    r = server["client"].post("/api/runs/k1/rerun")
    assert r.status_code == 400
    assert "no recorded launch" in r.json()["error"]


def test_log_merges_timeline_and_console(server, tmp_path):
    bundle = tmp_path / "b1"
    bundle.mkdir()
    (bundle / "journal.jsonl").write_text(
        '{"seq": 1, "ts": "2026-09-28T08:01:00+00:00", "phase": "cycle",'
        ' "event": "done", "detail": "ok"}\n')
    (bundle / "ops.jsonl").write_text(
        '{"op": "a1", "kind": "complete", "event": "start",'
        ' "at": "2026-09-28T08:00:00+00:00"}\n')
    (bundle / "console.log").write_text("2026-09-28T08:00:30+00:00 hi\n")
    runs.adopt_bundle(bundle, path=server["registry"])
    log = server["client"].get("/api/runs/b1/log").json()
    assert [e["source"] for e in log["timeline"]] == ["ops", "journal"]
    assert log["console"]["present"] is True
    assert log["console"]["lines"] == ["2026-09-28T08:00:30+00:00 hi"]
    assert log["launcher"] == {"present": False, "lines": []}


def test_usage_aggregates_registry(server):
    runs.register_start("k1", "n", "b", path=server["registry"])
    usage = server["client"].get("/api/usage").json()["usage"]
    assert usage[0]["key"] == "k1"


def test_experiments_lists_acceptance_verdicts(server, tmp_path):
    acc = tmp_path / "acceptance" / "2026-01-01"
    acc.mkdir(parents=True)
    (acc / "manifest.json").write_text(json.dumps({
        "generated_at": "t", "repo_rev": "abc123",
        "verdicts": [{"scenario": "s1", "status": "pass", "detail": "d"}]}))
    exps = server["client"].get("/api/experiments").json()["experiments"]
    assert exps[0]["date"] == "2026-01-01"
    assert exps[0]["verdicts"][0]["scenario"] == "s1"


@pytest.mark.live  # needs docker + broker + image build; unavailable on CI runners
def test_pause_unpause_live_container(server, monkeypatch):
    """Real docker pause cycle around a trivial launcher run.

    Needs docker, the canary-gate broker, and an image build (~minutes).
    """
    if shutil.which("docker") is None:
        pytest.skip("needs docker")
    gate = os.environ.get("CANARY_GATE_NAME", "canary-gate")
    try:
        out = subprocess.run(
            ["docker", "container", "inspect", "-f", "{{.State.Running}}", gate],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("needs a running docker daemon")
    if out.returncode != 0 or out.stdout.strip() != "true":
        pytest.skip(f"needs the {gate} broker container running")
    monkeypatch.setenv("ALLOW_DIRTY", "1")  # test checkout is mid-change
    c = server["client"]
    launcher = str(runs.ROOT / "scripts" / "container_run.sh")
    r = c.post("/api/runs", json={
        "name": "pause probe", "brief": "containment: pause/unpause",
        "argv": ["bash", launcher, "exec", "sleep", "25"]})
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    container = ""
    end = time.monotonic() + 300
    while time.monotonic() < end:
        row = c.get(f"/api/runs/{key}").json()
        container = row.get("container") or ""
        if container:
            break
        time.sleep(2)
    assert container, "launcher never assigned a container"
    try:
        assert c.post(f"/api/runs/{key}/pause").json()["status"] == "paused"
        assert c.post(f"/api/runs/{key}/unpause").json()["status"] == "running"
    finally:
        subprocess.run(["docker", "stop", "-t", "5", container],
                       capture_output=True, timeout=30)
    row = _wait_for(c, key, "failed", timeout=120)
    assert row["status"] == "failed"  # stopped mid-run: honest non-zero exit


def test_research_endpoint_serves_papers_claims_synthesis(server, tmp_path):
    bundle = tmp_path / "r1"
    (bundle / "iterations").mkdir(parents=True)
    (bundle / "assessments").mkdir(parents=True)
    (bundle / "run.json").write_text(json.dumps({
        "seed": "seed?", "stopped": "converged", "model": "stub",
        "usage": {"model_calls": 3, "tokens": 99},
        "unanswered": ["next?"]}))
    (bundle / "synthesis.md").write_text("# head\n\n**Headline answer:** yes.\n")
    (bundle / "retrieval.jsonl").write_text(
        json.dumps({"question": "seed?", "papers": [
            {"ref": "arxiv:1", "title": "T1", "year": 2026, "source": "arxiv",
             "doi": "", "oa_url": "https://example.test/1",
             "abstract": "abstract one"},
            {"ref": "arxiv:1", "title": "T1", "year": 2026, "source": "arxiv",
             "doi": "", "oa_url": "https://example.test/1",
             "abstract": "abstract one"}]}) + "\n")
    (bundle / "iterations" / "iter1.json").write_text(json.dumps({
        "n": 1, "kind": "review", "question": "seed?",
        "papers": ["T1"],
        "claims": [{"id": "c1", "text": "claim one", "support": "supported",
                    "scope": "abstract", "uncertainty": "",
                    "evidence": [{"paper": 1, "span": "span one"}]}],
        "validation": {"claims_parsed": 1, "batches": 1}}))
    (bundle / "iterations" / "iter1-review.md").write_text("# review\n\nbody\n")
    (bundle / "assessments" / "a1.json").write_text(json.dumps({
        "id": "a1", "ts": "2026-09-28T08:02:00+00:00",
        "outcome": "mid-run", "doc_markdown": "## worked\n- x\n"}))
    (bundle / "journal.jsonl").write_text(
        '{"seq": 1, "ts": "t", "phase": "cycle", "event": "start",'
        ' "Detail": "seed=seed?"}\n')
    runs.adopt_bundle(bundle, path=server["registry"])
    r = server["client"].get("/api/runs/r1/research")
    assert r.status_code == 200, r.text
    d = r.json()["research"]
    assert d["seed"] == "seed?" and d["stopped"] == "converged"
    assert "Headline answer" in d["synthesis_md"]
    assert d["papers_total"] == 2 and len(d["papers"]) == 1  # deduped by ref
    assert d["papers"][0]["abstract"] == "abstract one"
    assert d["iterations"][0]["claims"][0]["text"] == "claim one"
    assert "body" in d["iterations"][0]["review_md"]
    assert d["assessments"][0]["id"] == "a1"
    assert d["decisions"][0]["detail"] == "seed=seed?"  # Detail normalized
    assert d["unanswered"] == ["next?"]


def test_research_endpoint_missing_bundle_is_empty_not_500(server):
    runs.register_start("k9", "n", "b", bundle="/nonexistent",
                        path=server["registry"])
    r = server["client"].get("/api/runs/k9/research")
    assert r.status_code == 200
    assert server["client"].get("/api/runs/nope/research").status_code == 404
