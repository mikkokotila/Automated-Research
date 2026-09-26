import json
import subprocess

import httpx
import pytest

from canary import github_ops as ghmod
from canary import revise as impmod
from canary.journal import Journal
from canary.assess import Proposal, AssessmentDoc


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch):
    monkeypatch.setenv("CANARY_SANDBOXED", "1")


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
