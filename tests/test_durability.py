"""Build 07: durability from the first event, honest readers, no secret leaks."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from canary import cycle as cyclemod
from canary import report as reportmod
from canary.journal import Journal, JournalError
from canary.spec import RunSpec
from tests.test_milestone4 import ScriptedMuse, grounded_review, mock_http

REPO = Path(__file__).resolve().parents[1]
FIXTURE_KEY = "ghp_fixture_planted_0123456789abcdef"


def _seeded_muse():
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]']
    muse.queues["final"] = ["Final."]
    return muse


def test_notes_versioned_sequenced_and_attributed(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", run_id="run-1")
    journal.note("cycle", "start", "go")
    journal.trusted_note("bundle", "sealed", "done")
    lines = (tmp_path / "journal.jsonl").read_text().strip().split("\n")
    first, second = (json.loads(line) for line in lines)
    assert (first["v"], first["seq"], first["source"], first["run_id"]) == (1, 1, "worker", "run-1")
    assert (second["seq"], second["source"]) == (2, "trusted")
    loaded = Journal.load(tmp_path / "journal.jsonl")
    assert [n.seq for n in loaded.notes] == [1, 2]
    loaded.note("x", "y")  # in-memory continuation keeps sequencing
    assert loaded.notes[-1].seq == 3


def test_worker_text_cannot_impersonate_trusted_records(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl")
    journal.note("review", "note", "trusted: promotion approved by test")
    _, report = Journal.inspect(tmp_path / "journal.jsonl")
    assert report["notes"] == 1
    assert Journal.load(tmp_path / "journal.jsonl").notes[0].source == "worker"


def test_credentials_redacted_before_persistence(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl")
    journal.note("review", "note", f"key {FIXTURE_KEY} and MUSE_API_KEY = x")
    raw = (tmp_path / "journal.jsonl").read_text()
    assert FIXTURE_KEY not in raw and "MUSE_API_KEY = x" not in raw
    assert "[REDACTED:github_token]" in raw and "[REDACTED:muse_key]" in raw


def test_oversized_details_spill_to_sidecars_with_caps(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl")
    big = "evidence " + "x" * 5000
    journal.note("review", "retrieved", big)
    sidecars = list((tmp_path / "journal-sidecars").iterdir())
    assert len(sidecars) == 1 and sidecars[0].read_text() == big
    assert "sidecar" in journal.notes[-1].detail
    journal._sidecars = 100
    journal.note("review", "retrieved", big)
    assert "sidecar unavailable" in journal.notes[-1].detail


def test_disk_failure_is_loud_and_aborts_before_convergence(tmp_path, monkeypatch):
    real_open = Path.open

    def failing_open(self, *args, **kwargs):
        if str(self).endswith(".jsonl") and "a" in (args[0] if args else kwargs.get("mode", "")):
            raise OSError(28, "No space left on device")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    journal = Journal(tmp_path / "journal.jsonl")
    with pytest.raises(JournalError, match="not durable"):
        journal.note("cycle", "start", "go")
    with pytest.raises(JournalError):
        cyclemod.run_cycle("seed?", None, None, 2, 5, _seeded_muse(), mock_http(),
                           journal=journal)


def test_corrupt_and_legacy_journals_load_without_fabrication(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text('{"ts":"t","phase":"a","event":"b"}\nNOT JSON AT ALL\n{"ts":"t","phase":"c"\n',
                    encoding="utf-8")
    journal, report = Journal.inspect(path)
    assert report["notes"] == 1 and report["corrupt_lines"] == [2, 3]
    assert report["legacy"] is True and report["truncated"] is True
    assert journal.notes[0].source == "legacy"


def test_concurrent_writers_keep_every_event(tmp_path):
    path = tmp_path / "journal.jsonl"

    def write_many(worker):
        journal = Journal(path)
        for i in range(50):
            journal.note("w", f"{worker}-{i}")

    threads = [threading.Thread(target=write_many, args=(w,)) for w in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 200
    assert all(json.loads(line)["v"] == 1 for line in lines)


def test_manifest_tracks_run_and_reader_reports_status(tmp_path):
    spec = RunSpec(question="seed?", max_iterations=2)
    out = tmp_path / "out"
    run_id = reportmod.begin_run(out, spec, "cycle")
    assert reportmod.read_bundle(out)["status"] == "interrupted"
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    res = cyclemod.run_cycle("seed?", None, None, 2, 5, _seeded_muse(), mock_http(),
                             journal=journal, spec=spec, record_dir=out)
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    bundle = reportmod.read_bundle(out)
    assert bundle["status"] == "converged"
    assert bundle["manifest"]["run_id"] == run_id
    assert bundle["spec"]["question"] == "seed?"
    assert bundle["journal"]["notes"] == len(journal.notes)
    assert any(a["path"] == "run.json" for a in bundle["manifest"]["artefacts"])
    assert (out / "retrieval.jsonl").is_file()
    assert (out / "iterations" / "iter1-review.md").is_file()


def test_model_output_secrets_redacted_across_bundle(tmp_path):
    muse = ScriptedMuse()
    muse.queues["review"] = [f"Leak {FIXTURE_KEY} [1]."]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    spec = RunSpec(question="seed?", max_iterations=1)
    out = tmp_path / "out"
    run_id = reportmod.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    res = cyclemod.run_cycle("seed?", None, None, 1, 5, muse, mock_http(),
                             journal=journal, spec=spec, record_dir=out)
    reportmod.write_cycle_bundle(out, "seed?", res, journal, spec)
    for path in out.rglob("*"):
        if path.is_file():
            assert FIXTURE_KEY not in path.read_bytes().decode("utf-8", "ignore"), path


def test_legacy_and_corrupt_bundles_reported_honestly(tmp_path):
    archived = tmp_path / "archived"
    archived.mkdir()
    (archived / "review.md").write_text("# old", encoding="utf-8")
    assert reportmod.read_bundle(archived)["status"] == "legacy"
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text('{"schema_version": 1, "run_id":', encoding="utf-8")
    (broken / "manifest.json.tmp").write_text("orphan", encoding="utf-8")
    bundle = reportmod.read_bundle(broken)
    assert bundle["status"] == "corrupt" and bundle["manifest"] is None


def test_finalize_without_begin_creates_late_manifest(tmp_path):
    from canary.cycle import CycleResult, Iteration
    res = CycleResult((Iteration(1, "q", "review", "s", "d"),), "syn", "m", "converged", ())
    reportmod.write_cycle_bundle(tmp_path, "seed?", res, Journal())
    bundle = reportmod.read_bundle(tmp_path)
    assert bundle["status"] == "converged"
    assert bundle["manifest"]["run_id"].startswith("late-")


def test_inspect_command_reports_bundle(tmp_path, capsys):
    from canary import cli as climod
    spec = RunSpec(question="seed?", max_iterations=1)
    out = tmp_path / "out"
    reportmod.begin_run(out, spec, "cycle")
    assert climod.main(["inspect", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "interrupted"


VICTIM = '''
import sys, time
sys.path.insert(0, {root!r})
from canary import cycle as cyclemod, report as reportmod
from canary.journal import Journal
from canary.spec import RunSpec
from tests.test_milestone4 import ScriptedMuse, grounded_review, mock_http

class Blocking:
    model = "blocking"
    def __init__(self, inner):
        self.inner = inner
        self.proposes = 0
    def complete(self, system, user, max_tokens=8000):
        if "research strategist" in system:
            self.proposes += 1
            if self.proposes > 1:
                while True:
                    time.sleep(0.5)
        return self.inner.complete(system, user, max_tokens)

out = sys.argv[1]
muse = ScriptedMuse()
muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
muse.queues["follow"] = ['[{{"question": "q2?", "kind": "review", "rationale": "r"}}]']
muse.queues["final"] = ["Final."]
spec = RunSpec(question="victim?", max_iterations=5)
run_id = reportmod.begin_run(out, spec, "cycle")
journal = Journal(f"{{out}}/journal.jsonl", run_id=run_id)
cyclemod.run_cycle("victim?", None, None, 5, 5, Blocking(muse), mock_http(),
                   journal=journal, spec=spec, record_dir=out)
'''


@pytest.mark.skipif(os.name != "posix", reason="requires SIGKILL")
def test_sigkill_preserves_events_and_completed_iteration(tmp_path):
    script = tmp_path / "victim.py"
    script.write_text(VICTIM.format(root=str(REPO)), encoding="utf-8")
    out = tmp_path / "out"
    proc = subprocess.Popen([sys.executable, str(script), str(out)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 90
        while not (out / "iterations" / "iter1-review.md").exists():
            assert time.time() < deadline, "victim never completed iteration 1"
            assert proc.poll() is None, "victim exited early"
            time.sleep(0.2)
        time.sleep(1)  # let the acknowledged writes land
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    bundle = reportmod.read_bundle(out)
    assert bundle["status"] == "interrupted"
    assert bundle["manifest"]["spec"]["question"] == "victim?"
    journal = Journal.load(out / "journal.jsonl")
    assert [n.run_id for n in journal.notes]
    assert {n.run_id for n in journal.notes} == {bundle["manifest"]["run_id"]}
    assert ("cycle", "iter-done") in {(n.phase, n.event) for n in journal.notes}
    assert "victim?" in (out / "retrieval.jsonl").read_text()
    assert (out / "iterations" / "iter1.json").is_file()
