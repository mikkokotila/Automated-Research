"""Build 08: checkpoints survive crashes; resume continues honestly."""

import httpx
import pytest

from canary import cycle as cyclemod, report
from canary.cycle import OpLog, ResumeError
from canary.journal import Journal
from canary.spec import RunBudget, RunSpec
from tests.test_milestone4 import grounded_review


class ScriptedMuse:
    """Routes queued responses by system-prompt kind. Records all calls."""

    model = "scripted"

    def __init__(self):
        self.queues: dict[str, list[str]] = {"follow": [], "review": [], "narrate": [], "final": []}
        self.calls: list[str] = []

    def _kind(self, system: str) -> str:
        if "research strategist" in system:
            return "follow"
        if "precise research assistant" in system:
            return "review"
        if "careful data scientist" in system:
            return "narrate"
        return "final"

    def complete(self, system, user, max_tokens=8000):
        kind = self._kind(system)
        self.calls.append(kind)
        q = self.queues[kind]
        if not q:
            raise AssertionError(f"no scripted response for {kind}")
        return q.pop(0)


class CrashMuse:
    """Fault injection: raises KeyboardInterrupt on the Nth completion."""

    def __init__(self, inner: ScriptedMuse, crash_on: int):
        self._inner = inner
        self._n = 0
        self._crash_on = crash_on

    @property
    def model(self):
        return self._inner.model

    @property
    def calls(self):
        return self._inner.calls

    def complete(self, system, user, max_tokens=8000):
        self._n += 1
        if self._n == self._crash_on:
            raise KeyboardInterrupt("simulated crash")
        return self._inner.complete(system, user, max_tokens)


def mock_http_handler(req: httpx.Request) -> httpx.Response:
    if "openalex" in str(req.url):
        return httpx.Response(200, json={
            "results": [{
                "id": "W1", "title": "Study on X", "doi": "https://doi.org/10.1/x",
                "publication_year": 2023, "cited_by_count": 5,
                "authorships": [{"author": {"display_name": "A. Uthor"}}],
                "primary_location": {"source": {"display_name": "J X"}},
                "abstract_inverted_index": {"X": [0], "matters": [1]},
            }]
        })
    return httpx.Response(200, json={"data": []})


def mock_http() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(mock_http_handler))


def manifest(out):
    return report.read_bundle(out)["manifest"]


def fresh_run(out, muse, http, **kw):
    """A recorded in_progress run, exactly as the CLI starts one."""
    spec = RunSpec(question="seed?", max_iterations=3, max_papers=5, **kw)
    run_id = report.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    budget = RunBudget.from_spec(spec)
    res = cyclemod.run_cycle(
        spec.question, spec.csv, spec.target, spec.max_iterations, spec.max_papers,
        muse, http, journal=journal, spec=spec, budget=budget, record_dir=str(out),
    )
    return res, journal, spec


def crash_after_first_iteration(out) -> RunSpec:
    """Seed + one follow-up queued; the crash lands inside iteration 2."""
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]']
    muse.queues["final"] = ["Final."]
    res, _, spec = fresh_run(out, CrashMuse(muse, crash_on=3), mock_http())
    assert res.stopped.value == "cancelled"  # crash caught, run left in_progress
    assert [i.question for i in res.iterations] == ["seed?"]
    assert manifest(out)["status"] == "in_progress"
    return spec


def resume_muse():
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R2")]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    return muse


def test_crash_then_resume_completes_from_checkpoint(tmp_path):
    out = tmp_path / "run"
    crash_after_first_iteration(out)
    assert (out / "checkpoints" / "checkpoint-0000.json").exists()
    assert (out / "checkpoints" / "checkpoint-0001.json").exists()

    res, journal, _, target = cyclemod.resume_cycle(out, resume_muse(), mock_http())

    assert target == str(out)
    assert [i.n for i in res.iterations] == [1, 2]
    assert [i.question for i in res.iterations] == ["seed?", "q2?"]
    assert res.stopped.value == "converged"
    notes = [(n.phase, n.event) for n in journal.notes]
    assert ("resume", "continued") in notes
    assert ("resume", "ambiguous-op") in notes  # the crashed call never finished


def test_interrupted_plus_resumed_matches_uninterrupted(tmp_path):
    direct = ScriptedMuse()
    direct.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    direct.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]', "[]"]
    direct.queues["final"] = ["Final."]
    whole, _, _ = fresh_run(tmp_path / "whole", direct, mock_http())

    crash_after_first_iteration(tmp_path / "broken")
    resumed, _, _, _ = cyclemod.resume_cycle(tmp_path / "broken", resume_muse(), mock_http())

    assert [i.question for i in resumed.iterations] == [i.question for i in whole.iterations]
    assert [i.summary for i in resumed.iterations] == [i.summary for i in whole.iterations]
    assert resumed.synthesis == whole.synthesis
    assert resumed.stopped == whole.stopped


def test_resume_carries_spend_in_place_but_fork_restarts(tmp_path):
    out = tmp_path / "run"
    crash_after_first_iteration(out)

    resumed, _, _, _ = cyclemod.resume_cycle(out, resume_muse(), mock_http())
    # Only checkpointed spend carries; the in-flight attempt died with the crash
    # and is disclosed by the ambiguous-op note instead of being billed blindly.
    assert resumed.usage["model_calls"] == 5

    crash_after_first_iteration(tmp_path / "run2")
    forked, _, _, _ = cyclemod.resume_cycle(tmp_path / "run2", resume_muse(), mock_http(),
                                            fork_dir=tmp_path / "fork")
    assert forked.usage["model_calls"] == 3  # spends never transfer to a fork


def test_code_change_refuses_in_place_but_fork_accepts(tmp_path, monkeypatch):
    out = tmp_path / "run"
    crash_after_first_iteration(out)
    run_id = manifest(out)["run_id"]
    monkeypatch.setattr(report, "code_revision", lambda: "sha256:changed")

    with pytest.raises(ResumeError, match="worker code changed"):
        cyclemod.resume_cycle(out, resume_muse(), mock_http())

    fork = tmp_path / "fork"
    res, _, _, _ = cyclemod.resume_cycle(out, resume_muse(), mock_http(), fork_dir=fork)
    assert res.stopped.value == "converged"
    fork_manifest = manifest(fork)
    assert fork_manifest["forked_from"] == run_id
    assert fork_manifest["run_id"] != run_id
    assert report.read_checkpoints(fork)["code_hash"] == "sha256:changed"
    assert report.read_checkpoints(out)["code_hash"] != "sha256:changed"  # parent untouched


def test_input_change_refuses_in_place_but_fork_accepts(tmp_path):
    out = tmp_path / "run"
    csv = tmp_path / "d.csv"
    rows = ["f1,t"] + [f"{0.01 * i},{i % 2}" for i in range(60)]
    csv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    muse = ScriptedMuse()
    muse.queues["review"] = ["R1 [1]."]
    muse.queues["narrate"] = ["Narr."]
    fresh_run(out, CrashMuse(muse, crash_on=2), mock_http(), csv=str(csv), target="t")
    with csv.open("a", encoding="utf-8") as f:
        f.write("0.2,0\n")

    with pytest.raises(ResumeError, match="inputs changed"):
        cyclemod.resume_cycle(out, resume_muse(), mock_http())

    cont = ScriptedMuse()
    cont.queues["narrate"] = ["Narr."]
    cont.queues["follow"] = ["[]"]
    cont.queues["final"] = ["Final."]
    res, _, _, _ = cyclemod.resume_cycle(out, cont, mock_http(), fork_dir=tmp_path / "fork")
    assert res.stopped.value == "converged"


def test_finished_run_needs_fork(tmp_path):
    out = tmp_path / "run"
    muse = ScriptedMuse()
    muse.queues["review"] = ["R1 [1]."]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    res, journal, spec = fresh_run(out, muse, mock_http())
    report.write_cycle_bundle(out, spec.question, res, journal, spec)

    with pytest.raises(ResumeError, match="already converged"):
        cyclemod.resume_cycle(out, resume_muse(), mock_http())

    cont = ScriptedMuse()
    cont.queues["follow"] = ["[]"]
    cont.queues["final"] = ["Fork final."]
    res, _, _, target = cyclemod.resume_cycle(out, cont, mock_http(), fork_dir=tmp_path / "fork")
    assert res.stopped.value == "converged"
    assert manifest(target)["forked_from"] == manifest(out)["run_id"]


def test_legacy_and_non_cycle_bundles_refused(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "journal.jsonl").write_text('{"kind": "x", "event": "y", "detail": "z"}\n',
                                          encoding="utf-8")
    with pytest.raises(ResumeError, match="legacy bundle"):
        cyclemod.resume_cycle(legacy, resume_muse(), mock_http())

    review = tmp_path / "review"
    spec = RunSpec(question="q?")
    report.begin_run(review, spec, "review")
    with pytest.raises(ResumeError, match="cycle runs only"):
        cyclemod.resume_cycle(review, resume_muse(), mock_http())


def test_short_journal_refuses_resume(tmp_path):
    out = tmp_path / "run"
    crash_after_first_iteration(out)
    lines = (out / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    (out / "journal.jsonl").write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    with pytest.raises(ResumeError, match="shorter than the checkpoint"):
        cyclemod.resume_cycle(out, resume_muse(), mock_http())


def test_corrupt_checkpoint_files_are_skipped(tmp_path):
    out = tmp_path / "run"
    crash_after_first_iteration(out)
    (out / "checkpoints" / "checkpoint-0009.json").write_text("{not json", encoding="utf-8")
    (out / "checkpoints" / "checkpoint-0008.json").write_text('{"schema_version": 999, "seq": 8}',
                                                              encoding="utf-8")
    res, _, _, _ = cyclemod.resume_cycle(out, resume_muse(), mock_http())
    assert [i.question for i in res.iterations] == ["seed?", "q2?"]


def test_oplog_replay_marks_only_unfinished_ops_ambiguous(tmp_path):
    log = OpLog(tmp_path / "ops.jsonl")
    op1 = log.start("complete")
    log.finish(op1)
    log.mark_checkpoint(0)
    op2 = log.start("complete")
    log.finish(op2)
    op3 = log.start("complete")  # crash before any response
    finished, ambiguous = OpLog.replay(tmp_path / "ops.jsonl")
    assert finished == [f"complete:{op2}"]
    assert ambiguous == [f"complete:{op3}"]


def test_malformed_propose_retries_once_then_fails_honestly(tmp_path):
    muse = ScriptedMuse()
    muse.queues["review"] = ["R1 [1]."]
    muse.queues["follow"] = ["junk", "still junk", "junk", "still junk"]
    muse.queues["final"] = ["Final."]
    out = tmp_path / "run"
    res, journal, _ = fresh_run(out, muse, mock_http())
    assert res.stopped.value == "failed"
    assert muse.calls.count("follow") == 4  # two rounds, one retry each
    events = [(n.phase, n.event) for n in journal.notes]
    assert ("cycle", "propose-malformed") in events
    assert ("cycle", "propose-unusable") in events


def test_malformed_then_valid_propose_recovers(tmp_path):
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ["junk", '[{"question": "q2?", "kind": "review", "rationale": "r"}]',
                             "[]"]
    muse.queues["final"] = ["Final."]
    res, _, _ = fresh_run(tmp_path / "run", muse, mock_http())
    assert res.stopped.value == "converged"
    assert [i.question for i in res.iterations] == ["seed?", "q2?"]


def test_diagnose_followups_taxonomy():
    assert cyclemod.diagnose_followups("[]") == "empty"
    assert cyclemod.diagnose_followups("nothing worth doing") == "malformed"
    assert cyclemod.diagnose_followups('[{"question": "q?", "kind": "review"}]') == "ok"
    assert cyclemod.diagnose_followups('[{"question": "q?", "kind": "bogus"}]') == "malformed"


def test_cli_resume_end_to_end(tmp_path, monkeypatch, capsys):
    from canary import cli

    out = tmp_path / "run"
    crash_after_first_iteration(out)
    monkeypatch.setattr(cli, "MuseClient", lambda *a, **k: resume_muse())
    real_client = httpx.Client  # the CLI builds its own client: keep it offline
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(mock_http_handler)))
    assert cli.main(["resume", str(out)]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed.endswith("synthesis.md")
    assert manifest(out)["status"] == "converged"


def test_cli_resume_refusal_is_exit_2(tmp_path, monkeypatch, capsys):
    from canary import cli

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "journal.jsonl").write_text('{"kind": "x", "event": "y", "detail": "z"}\n',
                                          encoding="utf-8")
    monkeypatch.setattr(cli, "MuseClient", lambda *a, **k: resume_muse())
    assert cli.main(["resume", str(legacy)]) == 2
    assert "cannot resume" in capsys.readouterr().err


def test_file_hash_marks_missing_inputs():
    assert report.file_hash(None) == "none"
    assert report.file_hash("/no/such/file.csv").startswith("missing:")
