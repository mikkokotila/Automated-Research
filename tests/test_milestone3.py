import httpx
import numpy as np
import pandas as pd
import pytest

from canary import cycle as cyclemod, report


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


def mock_http() -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
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

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture()
def csv_path(tmp_path):
    rng = np.random.RandomState(0)
    df = pd.DataFrame({"f1": rng.rand(60), "f2": rng.rand(60)})
    df["t"] = (df["f1"] > 0.5).astype(int)
    p = tmp_path / "d.csv"
    p.write_text(df.to_csv(index=False), encoding="utf-8")
    return str(p)


# --- parsing ---


def test_parse_followups_valid_and_capped():
    items = [{"question": f"q{i}?", "kind": "review", "rationale": "r"} for i in range(5)]
    import json

    got = cyclemod.parse_followups("```json\n" + json.dumps(items) + "\n```")
    assert len(got) == 3 and got[0].question == "q0?"


def test_parse_followups_rejects_garbage():
    assert cyclemod.parse_followups("no json here") == []
    assert cyclemod.parse_followups("[not valid") == []
    assert cyclemod.parse_followups('[{"question": "q?", "kind": "dance"}]') == []
    assert cyclemod.parse_followups('{"question": "q?"}') == []


# --- cycle behavior ---


def test_cycle_converges_on_empty_followups():
    muse = ScriptedMuse()
    muse.queues["review"] = ["Review text [1]."]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final synthesis (iter 1)."]
    res = cyclemod.run_cycle("seed question?", None, None, 3, 5, muse, mock_http())
    assert len(res.iterations) == 1 and res.stopped == "converged"
    assert res.iterations[0].kind == "review" and res.unanswered == ()


def test_cycle_runs_followup_then_converges():
    muse = ScriptedMuse()
    muse.queues["review"] = ["First [1].", "Second [1]."]
    muse.queues["follow"] = [
        '[{"question": "deeper angle?", "kind": "review", "rationale": "why"}]',
        "[]",
    ]
    muse.queues["final"] = ["Final (iter 1) (iter 2)."]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http())
    assert [i.question for i in res.iterations] == ["seed?", "deeper angle?"]
    assert res.stopped == "converged"


def test_cycle_dedupes_reproposed_questions():
    muse = ScriptedMuse()
    muse.queues["review"] = ["Only [1]."]
    muse.queues["follow"] = ['[{"question": "SEED? ", "kind": "review", "rationale": "dup"}]']
    muse.queues["final"] = ["Final."]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http())
    assert len(res.iterations) == 1 and res.stopped == "converged"


def test_cycle_respects_max_iterations_and_tracks_unanswered():
    muse = ScriptedMuse()
    muse.queues["review"] = ["R1 [1].", "R2 [1]."]
    muse.queues["follow"] = [
        '[{"question": "q2?", "kind": "review", "rationale": "r"}, '
        '{"question": "q3?", "kind": "review", "rationale": "r"}]',
    ]
    muse.queues["final"] = ["Final with leftovers."]
    res = cyclemod.run_cycle("q1?", None, None, 2, 5, muse, mock_http())
    assert len(res.iterations) == 2 and res.stopped == "max_iterations"
    assert res.unanswered == ("q3?",)


def test_cycle_with_dataset_runs_analyze_seed(csv_path):
    muse = ScriptedMuse()
    muse.queues["review"] = ["Lit [1]."]
    muse.queues["narrate"] = ["Data findings."]
    muse.queues["follow"] = ["[]", "[]"]
    muse.queues["final"] = ["Combined final."]
    res = cyclemod.run_cycle("seed?", csv_path, "t", 3, 5, muse, mock_http())
    assert [i.kind for i in res.iterations] == ["review", "analyze"]
    assert res.stopped == "converged"


def test_cycle_without_dataset_skips_analyze_followups():
    muse = ScriptedMuse()
    muse.queues["review"] = ["Lit [1]."]
    muse.queues["follow"] = ['[{"question": "needs data?", "kind": "analyze", "rationale": "r"}]']
    muse.queues["final"] = ["Final."]
    res = cyclemod.run_cycle("seed?", None, None, 3, 5, muse, mock_http())
    assert len(res.iterations) == 1 and res.stopped == "converged"


def test_cycle_validates_inputs():
    muse = ScriptedMuse()
    with pytest.raises(ValueError, match="max_iterations"):
        cyclemod.run_cycle("q?", None, None, 9, 5, muse, mock_http())
    with pytest.raises(ValueError, match="together"):
        cyclemod.run_cycle("q?", "d.csv", None, 2, 5, muse, mock_http())


def test_cycle_bundle(tmp_path):
    it = cyclemod.Iteration(n=1, question="q?", kind="review", summary="s", detail="# D", provenance={"a": 1})
    res = cyclemod.CycleResult((it,), "synth", "m", "converged", ("left?",))
    out = report.write_cycle_bundle(tmp_path, "seed?", res)
    assert (out / "synthesis.md").read_text().count("left?") == 1
    assert (out / "iterations" / "iter1-review.md").exists()
    assert (out / "run.json").exists()
