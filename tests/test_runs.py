"""Run registry: roundtrips and defensive bundle parsing."""
import json
import subprocess
import sys

import pytest

from canary import cli as climod
from canary import runs


@pytest.fixture()
def registry(tmp_path):
    return tmp_path / "registry.json"


def _bundle(tmp_path, name="b1"):
    b = tmp_path / name
    (b / "iterations").mkdir(parents=True)
    (b / "manifest.json").write_text(json.dumps({
        "run_id": "r1", "kind": "cycle", "status": "converged",
        "started_at": "2026-09-28T08:00:00+00:00",
        "finished_at": "2026-09-28T08:05:00+00:00",
        "spec": {"question": "seed?", "profile": "dev", "maintenance": True}}))
    (b / "run.json").write_text(json.dumps({
        "model": "m", "usage": {"model_calls": 3, "tokens": 100}}))
    (b / "journal.jsonl").write_text(
        '{"seq": 1, "ts": "2026-09-28T08:01:00+00:00", "phase": "cycle",'
        ' "event": "iter-done", "detail": "n=1"}\n'
        '{"seq": 2, "ts": "2026-09-28T08:02:00+00:00", "phase": "cycle",'
        ' "event": "revised", "detail": "kept=1 reverted=0 skipped=0"}\n')
    (b / "iterations" / "iter1.json").write_text(json.dumps(
        {"n": 1, "kind": "review", "question": "seed?"}))
    return b


def test_register_start_stores_launch_argv(registry):
    argv = ["bash", "scripts/container_run.sh", "cycle", "seed with spaces?"]
    row = runs.register_start("k1", "n", "b", path=registry, launch_argv=argv)
    assert row["launch_argv"] == argv
    assert runs.get("k1", path=registry)["launch_argv"] == argv
    row2 = runs.register_start("k2", "n", "b", path=registry)
    assert row2["launch_argv"] == []  # old callers omit it


def test_register_script_launch_argv(tmp_path, monkeypatch):
    reg = tmp_path / "registry.json"
    monkeypatch.setenv("CANARY_RUNS_REGISTRY", str(reg))
    script = str(runs.ROOT / "scripts" / "register_run.py")
    argv = ["bash", "scripts/container_run.sh", "cycle", "seed with spaces?"]
    base = [sys.executable, script, "start", "--key", "s1", "--name", "n",
            "--brief", "b", "--launch", "LOSSY"]
    r = subprocess.run(base + ["--launch-argv", json.dumps(argv)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert runs.get("s1", path=reg)["launch_argv"] == argv
    plain = [sys.executable, script, "start", "--key", "s0", "--name", "n",
             "--brief", "b"]
    r = subprocess.run(plain, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0  # no --launch-argv still works
    assert runs.get("s0", path=reg)["launch_argv"] == []
    bad = [sys.executable, script, "start", "--key", "s2", "--name", "n",
           "--brief", "b", "--launch-argv", "{not json"]
    r = subprocess.run(bad, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert runs.get("s2", path=reg)["launch_argv"] == []  # invalid degrades


def test_start_finish_roundtrip(registry):
    row = runs.register_start("k1", "name", "brief", "cycle", "b1", "c1",
                              "launch cmd", path=registry)
    assert row["status"] == "running" and row["started_at"]
    assert runs.get("k1", path=registry)["name"] == "name"
    assert runs.register_finish("missing", path=registry) is None


def test_finish_merges_bundle_and_keeps_name(registry, tmp_path):
    bundle = _bundle(tmp_path)
    runs.register_start("k1", "my run", "my brief", path=registry)
    row = runs.register_finish("k1", "done", str(bundle), path=registry)
    assert row["name"] == "my run" and row["brief"] == "my brief"
    assert row["status"] == "converged" and row["ended_at"]  # bundle status wins
    assert row["run_id"] == "r1" and row["usage"] == {"model_calls": 3, "tokens": 100}
    assert row["tally"] == {"kept": 1, "reverted": 0, "skipped": 0}
    assert row["iterations"] == [{"n": 1, "kind": "review", "question": "seed?",
                                  "status": "done"}]


def test_adopt_names_and_parses(registry, tmp_path):
    row = runs.adopt_bundle(_bundle(tmp_path), path=registry)
    assert row["key"] == "b1" and row["status"] == "converged"
    assert "seed?" in row["brief"]  # backfilled brief names the seed


def test_parse_bundle_degrades_on_empty_dir(tmp_path):
    record = runs.parse_bundle(tmp_path / "empty")
    assert record["status"] == "unknown" and record["iterations"] == []
    assert record["improvements"] == [] and record["log_files"] == []


def test_improvements_merge_candidates_and_proposals(tmp_path):
    bundle = _bundle(tmp_path)
    cands = bundle / "assessments" / "candidates"
    cands.mkdir(parents=True)
    (cands / "p1.json").write_text(json.dumps(
        {"proposal_id": "p1", "target": "src/canary/a.py",
         "status": "kept", "detail": "green"}))
    (bundle / "assessments" / "assess-x.json").write_text(json.dumps(
        {"proposals": [{"id": "p1", "target": "src/canary/a.py", "change": "c"},
                       {"id": "p2", "target": "src/canary/b.py", "change": "d"}]}))
    got = runs.parse_bundle(bundle)["improvements"]
    assert [(g["id"], g["status"]) for g in got] == [("p1", "kept"),
                                                    ("p2", "not-evaluated")]
    assert all(g["pr_url"] is None and g["merged"] is False for g in got)


def test_load_ignores_corrupt_registry(tmp_path):
    bad = tmp_path / "registry.json"
    bad.write_text("not json", encoding="utf-8")
    assert runs.load(bad) == []
    assert runs.get("k", path=bad) is None


def test_cli_list_show_log_adopt(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "registry.json"
    monkeypatch.setenv("CANARY_RUNS_REGISTRY", str(reg))
    bundle = _bundle(tmp_path)
    assert climod.main(["runs", "adopt", "--bundle", str(bundle),
                        "--name", "n", "--brief", "b"]) == 0
    assert climod.main(["runs", "list"]) == 0
    out = capsys.readouterr().out
    assert "b1" in out and "converged" in out
    assert climod.main(["runs", "show", "b1"]) == 0
    assert climod.main(["runs", "log", "b1", "--tail", "5"]) == 0
    out = capsys.readouterr().out
    assert "journal:cycle/revised" in out and "console: no console.log" in out
    assert climod.main(["runs", "show", "nope"]) == 1


def test_cli_actions_without_daemon_hint_serve(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CANARY_RUNS_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("CANARY_RUNS_PORT", "9")  # nothing serves discard-port
    assert climod.main(["runs", "pause", "k1"]) == 1
    assert "canary runs serve" in capsys.readouterr().err
