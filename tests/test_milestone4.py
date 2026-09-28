import json
import subprocess

import httpx
import pytest

from canary import revise as impmod
from canary import cycle as cyclemod
from canary import assess as refmod
from canary.journal import Journal, emit


class ScriptedMuse:
    model = "scripted"

    def __init__(self):
        self.queues: dict[str, list[str]] = {
            "follow": [], "review": [], "narrate": [], "final": [],
            "assess": [], "diff": [],
        }

    def _kind(self, system: str) -> str:
        if "research strategist" in system:
            return "follow"
        if "precise research assistant" in system:
            return "review"
        if "careful data scientist" in system:
            return "narrate"
        if "reviewer" in system:
            return "assess"
        if "unified diff" in system:
            return "diff"
        return "final"

    def complete(self, system, user, max_tokens=8000):
        q = self.queues[self._kind(system)]
        if not q:
            raise AssertionError("no scripted response")
        return q.pop(0)


def grounded_review(prefix: str) -> str:
    """Build 16: scripted review with one anchored supported claim.

    Marker-only syntheses are ungrounded and trip the no-progress stop after
    two consecutive iterations; taxonomy fixtures that need convergence use
    this instead. The span anchors against the shared mock paper title.
    """
    return (f"{prefix} [1].\n```claims\n" + json.dumps([{
        "id": "c1", "text": "X was studied", "support": "supported",
        "evidence": [{"paper": 1, "span": "Study on X"}]}]) + "\n```")


DIFF_FOO = """--- a/src/canary/foo.py
+++ b/src/canary/foo.py
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


@pytest.fixture(autouse=True)
def _revision_trust(monkeypatch):
    """Explicit in-process seam: exercise revision logic with the gate open.

    Environment flags never authorize revision (see tests/test_trust_boundary.py);
    this monkeypatch is the only bypass and it cannot cross a process boundary.
    """
    monkeypatch.setattr(impmod, "require_revision_trust", lambda: None)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src" / "canary").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "src" / "canary" / "foo.py").write_text("X = 1\n", encoding="utf-8")
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
    emit(j, "cycle", "start", "seed")
    emit(None, "cycle", "start")  # null-safe
    p = j.save(tmp_path / "j.jsonl")
    assert len(Journal.load(p)) == 1


def test_journal_text_keeps_head_and_tail():
    j = Journal()
    for i in range(50):
        j.note("p", f"e{i}", "x" * 200)
    t = j.text(400)
    assert len(t) <= 400 and "elided" in t
    assert "e0" in t and "e49" in t  # start and end survive


def test_journal_appends_durably(tmp_path):
    p = tmp_path / "sub" / "j.jsonl"
    j = Journal(p)
    j.note("a", "b", "c")
    assert len(Journal.load(p)) == 1  # on disk before any explicit save


# --- assessment ---


def test_parse_assessment_valid_and_capped(tmp_path):
    tree = tmp_path / "tree"
    (tree / "src" / "canary" / "a.py").parent.mkdir(parents=True)
    (tree / "src" / "canary" / "a.py").write_text("# fixture\n")
    props = [{"id": f"p{i}", "target": "src/canary/a.py", "change": "c", "reason": "r"} for i in range(5)]
    doc = refmod.parse_assessment(json.dumps({"assessment": "# R", "proposals": props}),
                                  tree=tree)
    assert doc.markdown == "# R" and len(doc.proposals) == 3


def test_parse_assessment_garbage():
    doc = refmod.parse_assessment("just prose, no json")
    assert doc.markdown.startswith("just prose") and doc.proposals == ()


def test_assess_prompts_with_notes():
    muse = ScriptedMuse()
    muse.queues["assess"] = [json.dumps({"assessment": "R", "proposals": []})]
    doc = refmod.assess("note1", "outcome1", muse)
    assert doc.proposals == ()


# --- revision gate ---


def test_target_policy():
    assert impmod.target_allowed("src/canary/rank.py") is None
    for bad in ("tests/test_a.py", ".github/ci.yml", "Dockerfile", "uv.lock",
                "src/canary/revise.py", "/etc/x", "src/../evil.py", "README.md"):
        assert impmod.target_allowed(bad), bad


def test_diff_targets_parses():
    assert impmod.diff_targets(DIFF_FOO) == ["src/canary/foo.py"]


def test_apply_keeps_on_green(repo):
    prop = refmod.Proposal("p1", "src/canary/foo.py", "bump", "r")
    oc = impmod.apply_one(repo, prop, DIFF_FOO, ["true"], Journal())
    assert oc.kept and (repo / "src/canary/foo.py").read_text() == "X = 2\n"


def test_apply_reverts_on_red(repo):
    prop = refmod.Proposal("p1", "src/canary/foo.py", "bump", "r")
    oc = impmod.apply_one(repo, prop, DIFF_FOO, ["false"], Journal())
    assert oc.applied and not oc.kept
    assert (repo / "src/canary/foo.py").read_text() == "X = 1\n"


def test_apply_rejects_forbidden_target(repo):
    prop = refmod.Proposal("p1", "tests/test_x.py", "weaken", "evil")
    oc = impmod.apply_one(repo, prop, DIFF_TESTS, ["true"], Journal())
    assert not oc.applied and "forbidden" in oc.reason
    assert (repo / "tests/test_x.py").read_text() == "assert True\n"


def test_apply_rejects_oversize(repo):
    big = DIFF_FOO + "\n".join(f"# pad {i}" for i in range(400))
    prop = refmod.Proposal("p1", "src/canary/foo.py", "big", "r")
    oc = impmod.apply_one(repo, prop, big, ["true"], Journal())
    assert not oc.applied and "too large" in oc.reason


def test_round_aborts_on_dirty_tree(repo):
    (repo / "src/canary/foo.py").write_text("X = 9\n", encoding="utf-8")
    doc = refmod.AssessmentDoc("R", (refmod.Proposal("p1", "src/canary/foo.py", "c", "r"),))
    rep = impmod.revise_round(repo, doc, ScriptedMuse(), Journal(), ["true"])
    assert rep.skipped == 1 and rep.kept == 0


def test_round_aborts_on_red_baseline(repo):
    doc = refmod.AssessmentDoc("R", (refmod.Proposal("p1", "src/canary/foo.py", "c", "r"),))
    rep = impmod.revise_round(repo, doc, ScriptedMuse(), Journal(), ["false"])
    assert rep.skipped == 1 and rep.kept == 0


def test_revise_from_journal_stops_when_nothing_kept(repo):
    muse = ScriptedMuse()
    muse.queues["assess"] = [
        json.dumps({"assessment": "R1", "proposals": [
            {"id": "p1", "target": "src/canary/foo.py", "change": "bump", "reason": "r"}]}),
        json.dumps({"assessment": "R2", "proposals": []}),
    ]
    muse.queues["diff"] = [DIFF_FOO]
    doc, rep = impmod.revise_from_journal("notes", "ok", repo, muse, rounds=3, check_cmd=["true"])
    assert rep.kept == 1 and doc.markdown.endswith("R2")  # merged record prefixes its range
    assert (repo / "src/canary/foo.py").read_text() == "X = 2\n"


# --- cycle wiring ---


def test_cycle_takes_notes():
    muse = ScriptedMuse()
    muse.queues["review"] = ["R [1]."]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["F."]
    j = Journal()
    cyclemod.run_cycle("seed?", None, None, 2, 5, muse, mock_http(), journal=j)
    phases = {(n.phase, n.event) for n in j.notes}
    assert ("cycle", "start") in phases and ("review", "retrieved") in phases and ("cycle", "done") in phases


def test_cycle_maintenance_wires_mid_and_post(repo):
    muse = ScriptedMuse()
    muse.queues["review"] = ["R [1]."]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["F."]
    muse.queues["assess"] = [
        json.dumps({"assessment": "mid", "proposals": []}),
        json.dumps({"assessment": "post", "proposals": []}),
    ]
    j = Journal()
    res = cyclemod.run_cycle("seed?", None, None, 2, 5, muse, mock_http(), journal=j,
                           maintenance=True, repo_root=str(repo), check_cmd=["true"])
    assert res.stopped == "converged"
    events = [n.event for n in j.notes]
    assert "mid-revised" in events and "revised" in events


def test_cycle_maintenance_never_breaks_run(repo):
    muse = ScriptedMuse()  # empty queues: mid-run revise fails, must be contained
    muse.queues["review"] = ["R [1]."]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["F."]
    j = Journal()
    res = cyclemod.run_cycle("seed?", None, None, 2, 5, muse, mock_http(), journal=j,
                           maintenance=True, repo_root=str(repo), check_cmd=["true"])
    assert res.stopped == "converged"
    assert "mid-revise-failed" in [n.event for n in j.notes]
