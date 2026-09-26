import json
import subprocess

import httpx
import pytest

from autoresearch import improve as impmod
from autoresearch import loop as loopmod
from autoresearch import reflect as refmod
from autoresearch.journal import Journal, emit


class ScriptedMuse:
    model = "scripted"

    def __init__(self):
        self.queues: dict[str, list[str]] = {
            "follow": [], "review": [], "narrate": [], "final": [],
            "reflect": [], "diff": [],
        }

    def _kind(self, system: str) -> str:
        if "research strategist" in system:
            return "follow"
        if "precise research assistant" in system:
            return "review"
        if "careful data scientist" in system:
            return "narrate"
        if "self-critic" in system:
            return "reflect"
        if "unified diff" in system:
            return "diff"
        return "final"

    def complete(self, system, user, max_tokens=8000):
        q = self.queues[self._kind(system)]
        if not q:
            raise AssertionError("no scripted response")
        return q.pop(0)


DIFF_FOO = """--- a/src/autoresearch/foo.py
+++ b/src/autoresearch/foo.py
@@ -1 +1 @@
-X = 1
+X = 2
"""

DIFF_TESTS = """--- a/tests/test_x.py
+++ b/tests/test_x.py
@@ -1 +1 @@
-assert True
+assert False
"""


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src" / "autoresearch").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "src" / "autoresearch" / "foo.py").write_text("X = 1\n", encoding="utf-8")
    (r / "tests" / "test_x.py").write_text("assert True\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=r, capture_output=True, check=True)
    return r


def mock_http() -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        if "openalex" in str(req.url):
            return httpx.Response(200, json={
                "results": [{
                    "id": "W1", "title": "Study on X", "doi": "https://doi.org/10.1/x",
                    "publication_year": 2023, "cited_by_count": 5,
                    "authorships": [{"author": {"display_name": "A. Uthor"}}],
                    "primary_location": {"source": {"display_name": "J X"}},
                    "abstract_inverted_index": {"X": [0]},
                }]
            })
        return httpx.Response(200, json={"data": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- journal ---


def test_journal_roundtrip_and_emit(tmp_path):
    j = Journal()
    emit(j, "loop", "start", "seed")
    emit(None, "loop", "start")  # null-safe
    p = j.save(tmp_path / "j.jsonl")
    assert len(Journal.load(p)) == 1


def test_journal_text_tail_caps():
    j = Journal()
    for i in range(50):
        j.note("p", f"e{i}", "x" * 200)
    assert len(j.text(100)) == 100
    assert j.text(10**9).endswith(j.text(100))  # keeps the most recent notes


# --- reflection ---


def test_parse_reflection_valid_and_capped():
    props = [{"id": f"p{i}", "target": "src/autoresearch/a.py", "change": "c", "reason": "r"} for i in range(5)]
    doc = refmod.parse_reflection(json.dumps({"reflection": "# R", "proposals": props}))
    assert doc.markdown == "# R" and len(doc.proposals) == 3


def test_parse_reflection_garbage():
    doc = refmod.parse_reflection("just prose, no json")
    assert doc.markdown.startswith("just prose") and doc.proposals == ()


def test_reflect_prompts_with_notes():
    muse = ScriptedMuse()
    muse.queues["reflect"] = [json.dumps({"reflection": "R", "proposals": []})]
    doc = refmod.reflect("note1", "outcome1", muse)
    assert doc.proposals == ()


# --- improvement gate ---


def test_target_policy():
    assert impmod.target_allowed("src/autoresearch/rank.py") is None
    for bad in ("tests/test_a.py", ".github/ci.yml", "Dockerfile", "uv.lock",
                "src/autoresearch/improve.py", "/etc/x", "src/../evil.py", "README.md"):
        assert impmod.target_allowed(bad), bad


def test_diff_targets_parses():
    assert impmod.diff_targets(DIFF_FOO) == ["src/autoresearch/foo.py"]


def test_apply_keeps_on_green(repo):
    prop = refmod.Proposal("p1", "src/autoresearch/foo.py", "bump", "r")
    oc = impmod.apply_one(repo, prop, DIFF_FOO, ["true"], Journal())
    assert oc.kept and (repo / "src/autoresearch/foo.py").read_text() == "X = 2\n"


def test_apply_reverts_on_red(repo):
    prop = refmod.Proposal("p1", "src/autoresearch/foo.py", "bump", "r")
    oc = impmod.apply_one(repo, prop, DIFF_FOO, ["false"], Journal())
    assert oc.applied and not oc.kept
    assert (repo / "src/autoresearch/foo.py").read_text() == "X = 1\n"


def test_apply_rejects_forbidden_target(repo):
    prop = refmod.Proposal("p1", "tests/test_x.py", "weaken", "evil")
    oc = impmod.apply_one(repo, prop, DIFF_TESTS, ["true"], Journal())
    assert not oc.applied and "forbidden" in oc.reason
    assert (repo / "tests/test_x.py").read_text() == "assert True\n"


def test_apply_rejects_oversize(repo):
    big = DIFF_FOO + "\n".join(f"# pad {i}" for i in range(400))
    prop = refmod.Proposal("p1", "src/autoresearch/foo.py", "big", "r")
    oc = impmod.apply_one(repo, prop, big, ["true"], Journal())
    assert not oc.applied and "too large" in oc.reason


def test_round_aborts_on_dirty_tree(repo):
    (repo / "src/autoresearch/foo.py").write_text("X = 9\n", encoding="utf-8")
    doc = refmod.ReflectionDoc("R", (refmod.Proposal("p1", "src/autoresearch/foo.py", "c", "r"),))
    rep = impmod.improve_round(repo, doc, ScriptedMuse(), Journal(), ["true"])
    assert rep.skipped == 1 and rep.kept == 0


def test_round_aborts_on_red_baseline(repo):
    doc = refmod.ReflectionDoc("R", (refmod.Proposal("p1", "src/autoresearch/foo.py", "c", "r"),))
    rep = impmod.improve_round(repo, doc, ScriptedMuse(), Journal(), ["false"])
    assert rep.skipped == 1 and rep.kept == 0


def test_improve_from_journal_stops_when_nothing_kept(repo):
    muse = ScriptedMuse()
    muse.queues["reflect"] = [
        json.dumps({"reflection": "R1", "proposals": [
            {"id": "p1", "target": "src/autoresearch/foo.py", "change": "bump", "reason": "r"}]}),
        json.dumps({"reflection": "R2", "proposals": []}),
    ]
    muse.queues["diff"] = [DIFF_FOO]
    doc, rep = impmod.improve_from_journal("notes", "ok", repo, muse, rounds=3, check_cmd=["true"])
    assert rep.kept == 1 and doc.markdown == "R2"
    assert (repo / "src/autoresearch/foo.py").read_text() == "X = 2\n"


# --- loop wiring ---


def test_loop_takes_notes():
    muse = ScriptedMuse()
    muse.queues["review"] = ["R [1]."]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["F."]
    j = Journal()
    loopmod.run_loop("seed?", None, None, 2, 5, muse, mock_http(), journal=j)
    phases = {(n.phase, n.event) for n in j.notes}
    assert ("loop", "start") in phases and ("review", "retrieved") in phases and ("loop", "done") in phases


def test_loop_self_improve_wires_mid_and_post(repo):
    muse = ScriptedMuse()
    muse.queues["review"] = ["R [1]."]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["F."]
    muse.queues["reflect"] = [
        json.dumps({"reflection": "mid", "proposals": []}),
        json.dumps({"reflection": "post", "proposals": []}),
    ]
    j = Journal()
    res = loopmod.run_loop("seed?", None, None, 2, 5, muse, mock_http(), journal=j,
                           self_improve=True, repo_root=str(repo), check_cmd=["true"])
    assert res.stopped == "converged"
    events = [n.event for n in j.notes]
    assert "mid-improved" in events and "improved" in events


def test_loop_self_improve_never_breaks_run(repo):
    muse = ScriptedMuse()  # empty queues: mid-run improve fails, must be contained
    muse.queues["review"] = ["R [1]."]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["F."]
    j = Journal()
    res = loopmod.run_loop("seed?", None, None, 2, 5, muse, mock_http(), journal=j,
                           self_improve=True, repo_root=str(repo), check_cmd=["true"])
    assert res.stopped == "converged"
    assert "mid-improve-failed" in [n.event for n in j.notes]
