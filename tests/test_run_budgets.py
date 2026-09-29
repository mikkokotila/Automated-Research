"""Build 06: versioned run specs, hard budgets, cancellation, honest partials."""
import json

import pytest

from canary import cycle as cyclemod
from canary import report as reportmod
from canary.journal import Journal
from canary.muse_client import RequestBlocked
from canary.spec import (BudgetExhausted, Cancelled, InvalidSpec, RunBudget, RunSpec,
                         StopReason)
from tests.test_milestone4 import ScriptedMuse, grounded_review, mock_http


def _spec(**kw):
    args = {"question": "seed?", "max_iterations": 2}
    args.update(kw)
    return RunSpec(**args)


# --- spec validation: before any call ---


@pytest.mark.parametrize("kw", [
    {"question": "  "}, {"question": "x" * 2001}, {"version": 2},
    {"model": "other"}, {"csv": "a.csv"}, {"target": "t"},
    {"max_papers": 0}, {"max_papers": 25_001}, {"year_from": 1800},
    {"max_iterations": 0}, {"max_iterations": 6}, {"revise_rounds": -1},
    {"max_reseeds": -1}, {"max_reseeds": 4},
    {"max_model_calls": 0}, {"max_model_calls": 10_001},
    {"max_tokens": 0}, {"max_tokens": 200_000_001},
    {"wall_time_s": 0}, {"wall_time_s": 86_401}, {"finalize_calls": 6},
    {"out_dir": "  "},
])
def test_spec_rejects_invalid_values(kw):
    with pytest.raises(InvalidSpec):
        _spec(**kw)


def test_invalid_spec_is_a_value_error():
    assert issubclass(InvalidSpec, ValueError)
    with pytest.raises(ValueError):
        _spec(max_iterations=99)


def test_spec_roundtrip_and_unknown_fields_rejected():
    spec = _spec(csv="d.csv", target="t", maintenance=True)
    clone = RunSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
    assert clone == spec
    with pytest.raises(InvalidSpec):
        RunSpec.from_dict({"question": "x", "max_callz": 3})
    with pytest.raises(InvalidSpec):
        RunSpec.from_dict(["not", "an", "object"])


def test_no_unlimited_defaults():
    spec = RunSpec(question="x")
    assert 0 < spec.max_iterations <= 5 and 0 < spec.max_model_calls <= 10_000
    assert 0 < spec.max_tokens <= 200_000_000 and 0 < spec.wall_time_s <= 86_400


# --- budget tracker ---


def test_tracker_reserves_reconciles_and_carries_spend():
    now = [1000.0]
    budget = RunBudget.from_spec(_spec(max_model_calls=2, wall_time_s=60), clock=lambda: now[0])
    budget.reserve_call()
    budget.note_usage(10, 4)
    assert budget.usage_summary() == {"model_calls": 1, "tokens": 14, "tokens_reported": True}
    now[0] += 61
    with pytest.raises(BudgetExhausted, match="wall-time"):
        budget.reserve_call()
    carried = RunBudget.from_dict(budget.to_dict(), clock=lambda: now[0])
    assert carried.calls == 1 and carried.tokens == 14  # spend retained, cap not reset
    with pytest.raises(BudgetExhausted):
        carried.reserve_call()
        carried.reserve_call()  # second new call exceeds max_model_calls=2


def test_tracker_rejects_bad_usage_and_cancel_first():
    budget = RunBudget.from_spec(_spec())
    with pytest.raises(ValueError):
        budget.note_usage(-1, 0)
    with pytest.raises(ValueError):
        budget.note_usage(1.5, 0)
    budget.cancel()
    with pytest.raises(Cancelled):
        budget.check()


def test_tracker_is_exact_under_concurrent_reserve():
    import threading

    budget = RunBudget(1000, 10 ** 9, 3600)
    threads = [threading.Thread(target=lambda: [
        (budget.reserve_call(), budget.note_usage(10, 5)) for _ in range(100)])
        for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert budget.calls == 800 and budget.tokens == 800 * 15


def test_concurrent_reserve_never_overspends_calls():
    import threading

    budget = RunBudget(100, 10 ** 9, 3600)
    won: list[int] = []

    def worker():
        for _ in range(50):
            try:
                budget.reserve_call()
            except BudgetExhausted:
                pass
            else:
                won.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert budget.calls == 100 and len(won) == 100


def test_run_review_journals_phase_timings(tmp_path):
    import re

    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1")]
    journal = Journal(tmp_path / "journal.jsonl", run_id="r1")
    cyclemod.run_review("seed?", 5, mock_http(), muse, journal)
    events = [json.loads(line) for line in
              (tmp_path / "journal.jsonl").read_text().splitlines()]
    by_event = {e["event"]: e["detail"] for e in events}
    assert re.fullmatch(r"\d+ candidates in \d+\.\d+s", by_event["retrieved"])
    assert re.fullmatch(r"\d+ papers, cited \d+, \d+ calls in \d+\.\d+s",
                        by_event["synthesized"])


# --- run integration ---


def _seeded_muse():
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]']
    muse.queues["final"] = ["Final."]
    return muse


def test_exhaustion_saves_honest_partial_and_sends_nothing_more():
    muse = _seeded_muse()
    spec = _spec(max_iterations=5, max_model_calls=3)
    res = cyclemod.run_cycle("seed?", None, None, 5, 5, muse, mock_http(), spec=spec)
    assert res.stopped == StopReason.BUDGET_EXHAUSTED
    assert res.stopped == "budget_exhausted"  # reason compares as its value
    assert len(res.iterations) == 1 and res.unanswered == ("q2?",)
    assert res.synthesis == "Final."  # finalize reserve held back one call
    assert res.usage == {"model_calls": 3, "tokens": 0, "tokens_reported": False}
    assert muse.queues["review"] == [grounded_review("R2")]  # next review never sent


def test_exhaustion_without_finalize_allowance_keeps_partial_usable(tmp_path):
    muse = _seeded_muse()
    spec = _spec(max_iterations=5, max_model_calls=2)
    res = cyclemod.run_cycle("seed?", None, None, 5, 5, muse, mock_http(), spec=spec)
    assert res.stopped == "budget_exhausted" and res.synthesis == ""
    out = reportmod.write_cycle_bundle(tmp_path, "seed?", res, Journal(), spec)
    assert (out / "spec.json").is_file()
    run = json.loads((out / "run.json").read_text())
    assert run["stopped"] == "budget_exhausted" and run["spec"]["max_model_calls"] == 2
    assert run["usage"]["model_calls"] == 2
    assert "No final synthesis" in (out / "synthesis.md").read_text()


def test_cancel_before_start_raises_and_mid_run_partials():
    muse = _seeded_muse()
    budget = RunBudget.from_spec(_spec())
    budget.cancel()
    with pytest.raises(Cancelled):
        cyclemod.run_cycle("seed?", None, None, 2, 5, muse, mock_http(), budget=budget)

    class Cancelling:
        model = "cancelling"

        def __init__(self, inner, budget, after):
            self.inner, self.budget, self.after, self.n = inner, budget, after, 0

        def complete(self, system, user, max_tokens=8000):
            self.n += 1
            if self.n > self.after:
                self.budget.cancel()
            return self.inner.complete(system, user, max_tokens)

    muse2, budget2 = _seeded_muse(), RunBudget.from_spec(_spec())
    res = cyclemod.run_cycle("seed?", None, None, 5, 5, Cancelling(muse2, budget2, 1),
                             mock_http(), budget=budget2)
    assert res.stopped == "cancelled" and len(res.iterations) == 1


def test_provider_block_mid_run_partials_but_zero_progress_raises():
    class Flaky:
        model = "flaky"

        def __init__(self, inner):
            self.inner, self.n = inner, 0

        def complete(self, system, user, max_tokens=8000):
            self.n += 1
            if self.n == 1:
                return self.inner.complete(system, user, max_tokens)
            raise RequestBlocked("token_budget_exhausted")

    res = cyclemod.run_cycle("seed?", None, None, 5, 5, Flaky(_seeded_muse()), mock_http())
    assert res.stopped == "provider_blocked" and len(res.iterations) == 1

    class Blocked:
        model = "blocked"

        def complete(self, *a, **k):
            raise RequestBlocked("token_budget_exhausted")

    with pytest.raises(RequestBlocked):
        cyclemod.run_cycle("seed?", None, None, 2, 5, Blocked(), mock_http())


def test_empty_seed_evidence_is_insufficient_not_converged():
    import httpx

    def empty(request):
        return httpx.Response(200, json={"results": [], "data": []})

    spec = _spec(max_iterations=2, max_reseeds=0)
    res = cyclemod.run_cycle("seed?", None, None, 2, 5, _seeded_muse(),
                             httpx.Client(transport=httpx.MockTransport(empty)),
                             spec=spec)
    assert res.stopped == "insufficient_evidence"
    assert res.iterations == () and res.unanswered == ("seed?",)


def _reseed_http():
    import httpx

    def handler(req):
        url = str(req.url)
        if "openalex" in url and "dead" in url:
            return httpx.Response(200, json={"results": []})
        if "openalex" in url:
            return httpx.Response(200, json={"results": [{
                "id": "W1", "title": "Study on X", "doi": "https://doi.org/10.1/x",
                "publication_year": 2023, "cited_by_count": 5,
                "authorships": [{"author": {"display_name": "A. Uthor"}}],
                "primary_location": {"source": {"display_name": "J X"}},
                "abstract_inverted_index": {"X": [0]}}]})
        return httpx.Response(200, json={"entries": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _reseed_muse(pivots):
    muse = ScriptedMuse()
    muse.queues["reseed"] = list(pivots)
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    return muse


def test_reseed_pivots_when_seed_finds_nothing():
    journal = Journal()
    spec = _spec(max_iterations=2)
    res = cyclemod.run_cycle("dead seed?", None, None, 2, 5,
                             _reseed_muse(["lively pivot?"]),
                             _reseed_http(), journal=journal, spec=spec)
    assert res.stopped == "converged"
    assert [i.provenance["seed"] for i in res.iterations] == ["lively pivot?"]
    assert any(n.event == "reseed" and "lively pivot?" in n.detail
               for n in journal.notes)


def test_reseed_bounded_and_honest_when_pivot_also_empty():
    import httpx

    def empty(request):
        return httpx.Response(200, json={"results": [], "entries": []})

    journal = Journal()
    spec = _spec(max_iterations=2, max_reseeds=1)
    res = cyclemod.run_cycle("dead seed?", None, None, 2, 5,
                             _reseed_muse(["second dead end?"]),
                             httpx.Client(transport=httpx.MockTransport(empty)),
                             journal=journal, spec=spec)
    assert res.stopped == "insufficient_evidence"
    assert res.iterations == () and res.unanswered == ("second dead end?",)
    assert sum(n.event == "reseed" for n in journal.notes) == 1
    assert any(n.event == "reseed-skipped" and "exhausted" in n.detail
               for n in journal.notes)


def test_no_reseed_when_iterations_supported():
    journal = Journal()
    res = cyclemod.run_cycle("seed?", None, None, 2, 5, _seeded_muse(),
                             mock_http(), journal=journal)
    assert res.stopped == "converged"
    assert not any(n.event == "reseed" for n in journal.notes)


def test_reseed_rejects_dupe_then_accepts_fresh():
    journal = Journal()
    spec = _spec(max_iterations=2)
    res = cyclemod.run_cycle("dead seed?", None, None, 2, 5,
                             _reseed_muse(["dead seed?", "lively pivot?"]),
                             _reseed_http(), journal=journal, spec=spec)
    assert res.stopped == "converged"
    assert any(n.event == "reseed-rejected" for n in journal.notes)
    assert any(n.event == "reseed" and "lively pivot?" in n.detail
               for n in journal.notes)


def test_reseed_state_survives_checkpoints(tmp_path):
    import glob

    journal = Journal()
    spec = _spec(max_iterations=2)
    cyclemod.run_cycle("dead seed?", None, None, 2, 5,
                       _reseed_muse(["lively pivot?"]),
                       _reseed_http(), journal=journal, spec=spec,
                       record_dir=str(tmp_path))
    latest = sorted(glob.glob(str(tmp_path / "checkpoints" / "*.json")))[-1]
    state = json.load(open(latest))
    assert state["reseeds_used"] == 1
    assert state["seeds_tried"] == ["dead seed?", "lively pivot?"]
    assert state["active_seed"] == "lively pivot?"


def test_parse_reseed_skips_preamble_and_quotes():
    assert cyclemod._parse_reseed("Here is a pivot:\n\"lively pivot?\"\n") == "lively pivot?"
    assert cyclemod._parse_reseed("\n\n  \n") == ""
    assert cyclemod._parse_reseed("Note:\n") == ""


def test_nested_work_shares_one_tracker():
    muse, budget = _seeded_muse(), RunBudget.from_spec(_spec())
    res = cyclemod.run_cycle("seed?", None, None, 2, 5, muse, mock_http(), budget=budget)
    assert res.stopped == "converged"
    assert budget.calls == 4  # seed + q2 reviews, one propose, final synthesis
    assert res.usage["model_calls"] == 4


def test_cli_rejects_invalid_input_before_any_work(tmp_path, capsys):
    from canary import cli as climod
    rc = climod.main(["cycle", "seed?", "--max-iterations", "0", "--out", str(tmp_path / "o")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "invalid input" in err
    assert not (tmp_path / "o").exists()
    rc = climod.main(["cycle", "seed?", "--max-calls", "0", "--out", str(tmp_path / "o2")])
    assert rc == 2
    assert not (tmp_path / "o2").exists()


def test_cycle_bundle_usage_includes_post_assessment_spend(tmp_path, monkeypatch):
    """run.json usage must reconcile with the budget after --assess (Issue #56)."""
    import httpx

    from canary import cli as climod

    inner = ScriptedMuse()
    inner.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    inner.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]', "[]"]
    inner.queues["final"] = ["Final."]
    inner.queues["assess"] = [json.dumps({"assessment": "R", "proposals": []})]
    seen = {}

    from canary.muse_client import MuseClient

    class BudgetedMuse(MuseClient):
        """Emulates production accounting: every complete reserves + reports."""

        def __init__(self, inner, budget):
            self._inner = inner
            self.budget = budget
            self.calls = 0
            self.recorder = None

        @property
        def model(self):
            return "budgeted"

        def complete(self, system, user, max_tokens=8000):
            self.budget.reserve_call()
            out = self._inner.complete(system, user, max_tokens)
            self.budget.note_usage(10, 5)
            return out

    def factory(*a, **k):
        seen["budget"] = k["budget"]
        return BudgetedMuse(inner, k["budget"])

    monkeypatch.setattr(climod, "MuseClient", factory)
    http = mock_http()  # built before the patch: the CLI builds its own client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: http)
    out = tmp_path / "out"
    climod.cycle(_spec(out_dir=str(out)), assess=True)
    reported = json.loads((out / "run.json").read_text())["usage"]
    actual = seen["budget"].usage_summary()
    assert reported == actual  # stale snapshot misses the post-assess calls
    assert reported["model_calls"] > 0 and reported["tokens"] == reported["model_calls"] * 15
