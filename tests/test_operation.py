import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from canary import github_ops as ghmod
from canary import revise as impmod
from canary.journal import Journal
from canary.assess import Proposal, AssessmentDoc


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
    (r / "src" / "canary" / "foo.py").write_text("X = 1\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=r, capture_output=True, check=True)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], capture_output=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=r, capture_output=True, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=r, capture_output=True, check=True)
    return r


class FakeGitHubAPI:
    """Stateful mock: records calls, scripted check progression."""

    def __init__(self, checks_script=("success",)):
        self.calls: list[tuple[str, str]] = []
        self.checks_script = list(checks_script)
        self.bodies: list[dict] = []
        self.pr_merged = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        body = json.loads(req.content.decode() or "{}")
        self.bodies.append(body)
        if req.url.path.endswith("/issues") and req.method == "POST":
            return httpx.Response(201, json={"number": 7, "html_url": "https://gh/issues/7"})
        if req.url.path.endswith("/comments"):
            return httpx.Response(201, json={"id": 1})
        if req.url.path.endswith("/pulls") and req.method == "POST":
            return httpx.Response(201, json={
                "number": 9, "html_url": "https://gh/pull/9", "node_id": "PR_1",
                "head": {"sha": "abc123"}})
        if req.url.path.endswith("/pulls/9") and req.method == "GET":
            return httpx.Response(200, json={"merged": self.pr_merged, "state": "open", "head": {"sha": "abc123"}})
        if req.url.path.endswith("/check-runs"):
            verdict = self.checks_script[min(len([c for c in self.calls if c[1].endswith("/check-runs")]) - 1,
                                           len(self.checks_script) - 1)]
            if verdict == "pending":
                return httpx.Response(200, json={"check_runs": [{"name": "ci", "status": "in_progress"}]})
            return httpx.Response(200, json={"check_runs": [
                {"name": "ci", "status": "completed", "conclusion": verdict}]})
        if req.url.path.endswith("/merge"):
            self.pr_merged = True
            return httpx.Response(200, json={"merged": True})
        if req.url.path == "/graphql":
            return httpx.Response(200, json={"data": {"enablePullRequestAutoMerge": {
                "pullRequest": {"autoMergeRequest": {"enabledAt": "now"}}}}})
        if req.method == "DELETE":
            return httpx.Response(204, json={})
        return httpx.Response(404, json={})

    def client(self, repo="o/n"):
        http = httpx.Client(base_url=ghmod.API, transport=httpx.MockTransport(self.handler))
        return ghmod.GitHub("tok", repo, client=http)


# --- resolve ---


def test_resolve_token_and_repo(repo, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        ghmod.resolve_token()
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert ghmod.resolve_token() == "t"
    monkeypatch.setenv("GITHUB_REPO", "a/b")
    assert ghmod.resolve_repo(repo) == "a/b"
    monkeypatch.delenv("GITHUB_REPO")
    subprocess.run(["git", "remote", "set-url", "origin", "git@github.com:x/y.git"],
                   cwd=repo, capture_output=True, check=True)
    assert ghmod.resolve_repo(repo) == "x/y"


# --- issues + PRs ---


def test_issue_and_comment():
    api = FakeGitHubAPI()
    gh = api.client()
    issue = gh.create_issue("T", "B")
    assert (issue.number, issue.url) == (7, "https://gh/issues/7")
    gh.comment(7, "hi")
    assert ("POST", "/repos/o/n/issues/7/comments") in api.calls


def test_pr_lifecycle_merges_when_green():
    api = FakeGitHubAPI(checks_script=("pending", "success"))
    gh = api.client()
    pr = gh.create_pr("T", "B", head="auto/x")
    assert pr.number == 9 and pr.head_sha == "abc123"
    assert gh.wait_and_merge(pr, timeout_s=30, interval_s=0) == "merged"
    assert any("graphql" in p for _, p in api.calls)  # auto-merge attempted first
    gh.delete_branch("auto/x")
    assert ("DELETE", "/repos/o/n/git/refs/heads/auto/x") in api.calls


def test_wait_and_merge_rejects_red_checks():
    api = FakeGitHubAPI(checks_script=("failure",))
    gh = api.client()
    pr = gh.create_pr("T", "B", head="auto/x")
    with pytest.raises(RuntimeError, match="checks failed"):
        gh.wait_and_merge(pr, timeout_s=30, interval_s=0)


def test_enable_automerge_never_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    gh = ghmod.GitHub("t", "o/n", client=httpx.Client(base_url=ghmod.API, transport=httpx.MockTransport(handler)))
    assert gh.enable_automerge("PR_1") is False


def test_merge_not_yet_mergeable_returns_false():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(405, json={})

    gh = ghmod.GitHub("t", "o/n", client=httpx.Client(base_url=ghmod.API, transport=httpx.MockTransport(handler)))
    assert gh.merge(9) is False


def test_auth_failure_is_explicit():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    gh = ghmod.GitHub("bad", "o/n", client=httpx.Client(base_url=ghmod.API, transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="401"):
        gh.create_issue("T", "B")


# --- commit + publish ---


def test_commit_stage_refuses_strays(repo):
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "t.py").write_text("x\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected change"):
        ghmod.commit_stage_list(repo)


def test_commit_and_push_to_local_remote(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    (repo / "src" / "canary" / "foo.py").write_text("X = 2\n", encoding="utf-8")
    (repo / "runs").mkdir()
    (repo / "runs" / "r-journal.jsonl").write_text("{}\n", encoding="utf-8")
    files = ghmod.commit_and_push(repo, "auto/x", "auto msg", "fake")
    assert len(files) == 2
    out = subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=repo, capture_output=True, text=True)
    assert "auto/x" in out.stdout


def test_publish_round_full_flow(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    (repo / "src" / "canary" / "foo.py").write_text("X = 2\n", encoding="utf-8")  # kept patch
    api = FakeGitHubAPI()
    doc = AssessmentDoc("## R", (Proposal("p1", "src/canary/foo.py", "bump", "r"),))
    rep = impmod.ReviseReport(kept=1)
    rep.outcomes.append(impmod.PatchOutcome("p1", "src/canary/foo.py", True, True, "green"))
    pub = impmod.publish_round(repo, "rid1", doc, rep, Journal(), api.client(),
                               merge_timeout_s=30, merge_interval_s=0)
    assert pub.merged and pub.issue_number == 7 and pub.pr_url == "https://gh/pull/9"
    assert (repo / "runs" / "rid1-assessment.md").exists()
    assert (repo / "runs" / "rid1-journal.jsonl").exists()
    out = subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=repo, capture_output=True, text=True)
    assert "auto/revise-rid1" in out.stdout


def test_publish_round_issue_only_when_nothing_kept(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    api = FakeGitHubAPI()
    doc = AssessmentDoc("## R", ())
    pub = impmod.publish_round(repo, "rid2", doc, impmod.ReviseReport(), Journal(), api.client())
    assert pub.issue_number == 7 and pub.pr_url == "" and not pub.merged
    out = subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=repo, capture_output=True, text=True)
    assert "auto/" not in out.stdout


def test_publish_round_survives_merge_failure(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    (repo / "src" / "canary" / "foo.py").write_text("X = 2\n", encoding="utf-8")
    api = FakeGitHubAPI(checks_script=("failure",))
    doc = AssessmentDoc("## R", ())
    rep = impmod.ReviseReport(kept=1)
    pub = impmod.publish_round(repo, "rid3", doc, rep, Journal(), api.client(),
                               merge_timeout_s=30, merge_interval_s=0)
    assert not pub.merged and pub.pr_url != ""
    assert any("needs a human" in json.dumps(b) for b in api.bodies)


# --- container publish bridge (#66) ---


BRIDGE_DIFF = """diff --git a/src/canary/foo.py b/src/canary/foo.py
--- a/src/canary/foo.py
+++ b/src/canary/foo.py
@@ -1 +1 @@
-X = 1
+X = 2
"""

OLD_SHA = hashlib.sha256(b"X = 1\n").hexdigest()
NEW_SHA = hashlib.sha256(b"X = 2\n").hexdigest()


def _bridge_candidate(diff=BRIDGE_DIFF, status="kept", target="src/canary/foo.py",
                      old=OLD_SHA, new=NEW_SHA, **kw):
    entry = {
        "proposal_id": "p1", "target": target, "change": "bump", "reason": "r",
        "assessment_id": "a1", "status": status, "detail": "checks green",
        "manifest": {"base_rev": "rev-guest",
                     "diff_sha": "sha256:" + hashlib.sha256(diff.encode()).hexdigest(),
                     "files": [{"path": target, "op": "modified",
                                "old_sha": old, "new_sha": new}]},
        "raw_diff": diff,
    }
    entry.update(kw)
    return entry


def _bridge_bundle(tmp_path, candidates, run_id="guest-1", with_manifest=True):
    b = tmp_path / "bundle"
    cdir = b / "assessments" / "candidates"
    cdir.mkdir(parents=True)
    for name, entry in candidates.items():
        (cdir / name).write_text(json.dumps(entry, indent=2), encoding="utf-8")
    (b / "manifest.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    Journal().save(b / "journal.jsonl")
    if with_manifest:
        files, total = [], 0
        for p in sorted(b.rglob("*")):
            if p.is_file():
                raw = p.read_bytes()
                files.append({"path": str(p.relative_to(b)),
                              "sha256": hashlib.sha256(raw).hexdigest(),
                              "size": len(raw)})
                total += len(raw)
        (b / "manifest.canary.json").write_text(
            json.dumps({"count": len(files), "total_bytes": total, "files": files},
                       indent=2), encoding="utf-8")
    return b


@pytest.fixture()
def publish_repo(repo):
    """Fixture repo with the real export scanner wired in."""
    root = Path(__file__).resolve().parents[1]
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copy(root / "scripts" / "scan_export.py", scripts / "scan_export.py")
    pkg = repo / "src" / "canary"
    for src in (root / "src" / "canary").glob("*.py"):
        shutil.copy(src, pkg / src.name)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "scanner"], cwd=repo, capture_output=True,
                   check=True)
    return repo


def test_bridge_verifies_kept_candidate():
    cand = impmod.verify_bridge_candidate(_bridge_candidate())
    assert (cand.proposal_id, cand.target) == ("p1", "src/canary/foo.py")
    assert "X = 2" in cand.diff and cand.drift == ""


def test_bridge_refuses_tampered_and_malformed():
    bad_sha = _bridge_candidate()
    bad_sha["raw_diff"] += " "  # one byte breaks the manifest seal
    cases = {
        "tampered": bad_sha,
        "not-kept": _bridge_candidate(status="rejected"),
        "empty": _bridge_candidate(diff="  ", manifest={}),
        "missing-manifest": _bridge_candidate(manifest=None),
        "oversized": _bridge_candidate(diff="x" * (impmod.BRIDGE_MAX_DIFF_BYTES + 1)),
    }
    evil = _bridge_candidate(
        diff=BRIDGE_DIFF.replace("src/canary/foo.py", "tests/x.py"),
        target="tests/x.py")
    evil["manifest"]["files"] = [{"path": "tests/x.py", "op": "modified",
                                  "old_sha": "", "new_sha": ""}]
    cases["forbidden-target"] = evil
    deleted = _bridge_candidate(
        diff="diff --git a/src/canary/foo.py b/src/canary/foo.py\n"
             "deleted file mode 100644\n"
             "--- a/src/canary/foo.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-X = 1\n")
    cases["deletion"] = deleted
    cases["target-mismatch"] = _bridge_candidate(target="src/canary/other.py")
    for name, entry in cases.items():
        with pytest.raises(impmod.BridgeRefused):
            impmod.verify_bridge_candidate(entry)


def test_bridge_applies_exact_bytes(repo):
    cand = impmod.verify_bridge_candidate(_bridge_candidate())
    impmod.apply_bridge_candidates(repo, [cand])
    assert (repo / "src" / "canary" / "foo.py").read_text(encoding="utf-8") == "X = 2\n"
    assert cand.drift == ""  # byte-identical to the guest-tested result


def test_bridge_rolls_back_on_second_failure(repo):
    good = impmod.verify_bridge_candidate(_bridge_candidate())
    bad_diff = BRIDGE_DIFF.replace("X = 1", "X = 9").replace("X = 2", "X = 10")
    bad = impmod.verify_bridge_candidate(_bridge_candidate(diff=bad_diff))
    with pytest.raises(impmod.BridgeRefused, match="git apply failed"):
        impmod.apply_bridge_candidates(repo, [good, bad])
    assert (repo / "src" / "canary" / "foo.py").read_text(encoding="utf-8") == "X = 1\n"
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True).stdout == ""


def test_bridge_refuses_dirty_tree(repo):
    (repo / "src" / "canary" / "foo.py").write_text("X = 99\n", encoding="utf-8")
    cand = impmod.verify_bridge_candidate(_bridge_candidate())
    with pytest.raises(impmod.BridgeRefused, match="not clean"):
        impmod.apply_bridge_candidates(repo, [cand])
    assert (repo / "src" / "canary" / "foo.py").read_text(encoding="utf-8") == "X = 99\n"


def test_bridge_notes_result_drift_without_refusing(repo):
    entry = _bridge_candidate(new="0" * 64)  # manifest disagrees with reality
    cand = impmod.verify_bridge_candidate(entry)
    impmod.apply_bridge_candidates(repo, [cand])  # still applies: CI judges
    assert "differs from guest-tested bytes" in cand.drift


def test_bridge_notes_base_drift(repo):
    (repo / "src" / "canary" / "foo.py").write_text("X = 1\nY = 0\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "drift"], cwd=repo, capture_output=True,
                   check=True)
    drifted = BRIDGE_DIFF.replace("@@ -1 +1 @@\n-X = 1\n+X = 2\n",
                                  "@@ -1,2 +1,2 @@\n-X = 1\n+X = 2\n Y = 0\n")
    cand = impmod.verify_bridge_candidate(_bridge_candidate(diff=drifted))
    impmod.apply_bridge_candidates(repo, [cand])
    assert "base drift" in cand.drift


def test_bridge_full_flow(publish_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    bundle = _bridge_bundle(tmp_path, {"p1.json": _bridge_candidate()})
    api = FakeGitHubAPI()
    pub = impmod.publish_container_bundle(publish_repo, bundle, "brid1", Journal(),
                                           api.client(), merge_timeout_s=30,
                                           merge_interval_s=0)
    assert pub.merged and pub.issue_number == 7
    assert (publish_repo / "src" / "canary" / "foo.py").read_text(
        encoding="utf-8") == "X = 2\n"
    out = subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=publish_repo,
                         capture_output=True, text=True)
    assert "auto/revise-brid1" in out.stdout
    assert "guest-1" in json.dumps(api.bodies[0])


def test_bridge_refuses_unscanned_bundle(publish_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    bundle = _bridge_bundle(tmp_path, {"p1.json": _bridge_candidate()},
                            with_manifest=False)
    api = FakeGitHubAPI()
    with pytest.raises(impmod.BridgeRefused, match="scan failed"):
        impmod.publish_container_bundle(publish_repo, bundle, "brid2", Journal(),
                                         api.client())
    assert (publish_repo / "src" / "canary" / "foo.py").read_text(
        encoding="utf-8") == "X = 1\n"
    out = subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=publish_repo,
                         capture_output=True, text=True)
    assert "auto/" not in out.stdout
    assert api.calls == []  # refused before any GitHub side effect
