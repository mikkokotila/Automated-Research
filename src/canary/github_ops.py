"""GitHub ops: issues for journaling, PRs with auto-merge for patches.

The worker authenticates with a fine-grained PAT scoped to its own repo only
(contents + issues + pull requests). Token arrives via GITHUB_TOKEN env and is
never written to journals, issues, or logs.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

API = "https://api.github.com"
DEFAULT_REPO = "mikkokotila/Canary"
BOT_NAME = "canary-bot"
BOT_EMAIL = "canary-bot@users.noreply.github.com"
COMMIT_STAGE_PREFIXES = ("src/canary/", "runs/")


def resolve_token(env: dict | None = None) -> str:
    env = env if env is not None else os.environ
    token = env.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("no GITHUB_TOKEN in environment")
    return token


def resolve_repo(repo_root: str | Path | None = None) -> str:
    if os.environ.get("GITHUB_REPO"):
        return os.environ["GITHUB_REPO"]
    if repo_root is not None:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=repo_root,
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            m = re.search(r"github\.com[/:]([^/\s]+/[^/\s]+?)(?:\.git)?\s*$", r.stdout.strip())
            if m:
                return m.group(1)
    return DEFAULT_REPO


@dataclass(frozen=True)
class Issue:
    number: int
    url: str


@dataclass(frozen=True)
class Pull:
    number: int
    url: str
    node_id: str
    head_sha: str


class GitHub:
    def __init__(self, token: str, repo: str, client: httpx.Client | None = None) -> None:
        self.repo = repo
        self._client = client or httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Canary",
            },
            timeout=30.0,
        )

    def _post(self, path: str, payload: dict) -> dict:
        resp = self._client.post(path, json=payload)
        if resp.status_code == 401:
            raise RuntimeError("GitHub token rejected (401) — check GITHUB_TOKEN scope/expiry")
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str) -> dict:
        resp = self._client.get(path)
        if resp.status_code == 401:
            raise RuntimeError("GitHub token rejected (401) — check GITHUB_TOKEN scope/expiry")
        resp.raise_for_status()
        return resp.json()

    def create_issue(self, title: str, body: str) -> Issue:
        d = self._post(f"/repos/{self.repo}/issues", {"title": title, "body": body})
        return Issue(number=d["number"], url=d["html_url"])

    def comment(self, number: int, body: str) -> None:
        self._post(f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def create_pr(self, title: str, body: str, head: str, base: str = "main") -> Pull:
        d = self._post(f"/repos/{self.repo}/pulls", {"title": title, "body": body, "head": head, "base": base})
        return Pull(number=d["number"], url=d["html_url"], node_id=d["node_id"], head_sha=d["head"]["sha"])

    def get_pr(self, number: int) -> dict:
        return self._get(f"/repos/{self.repo}/pulls/{number}")

    def check_runs(self, sha: str) -> list[dict]:
        d = self._get(f"/repos/{self.repo}/commits/{sha}/check-runs")
        return d.get("check_runs", [])

    def enable_automerge(self, node_id: str) -> bool:
        """Best-effort auto-merge flag; False when unsupported. Never raises."""
        try:
            d = self._post("/graphql", {
                "query": "mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:SQUASH}){pullRequest{autoMergeRequest{enabledAt}}}}",
                "variables": {"id": node_id},
            })
            req = (d.get("data") or {}).get("enablePullRequestAutoMerge") or {}
            return bool(((req.get("pullRequest") or {}).get("autoMergeRequest") or {}).get("enabledAt"))
        except Exception:
            return False

    def merge(self, number: int) -> bool:
        try:
            d = self._post(f"/repos/{self.repo}/pulls/{number}/merge", {"merge_method": "squash"})
            return bool(d.get("merged"))
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 405:  # not mergeable yet
                return False
            raise

    def delete_branch(self, branch: str) -> None:
        resp = self._client.delete(f"/repos/{self.repo}/git/refs/heads/{branch}")
        if resp.status_code not in (204, 404, 422):
            resp.raise_for_status()

    def wait_and_merge(self, pr: Pull, timeout_s: float = 900, interval_s: float = 20) -> str:
        """Enable auto-merge, then ensure merge happens. Returns 'merged' or raises."""
        self.enable_automerge(pr.node_id)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state = self.get_pr(pr.number)
            if state.get("merged"):
                return "merged"
            if state.get("state") == "closed":
                raise RuntimeError(f"PR #{pr.number} closed without merge")
            runs = self.check_runs(state["head"]["sha"])
            if runs and all(r.get("status") == "completed" for r in runs):
                bad = [r for r in runs if r.get("conclusion") not in ("success", "neutral", "skipped")]
                if bad:
                    names = ", ".join(r.get("name", "?") for r in bad)
                    raise RuntimeError(f"PR #{pr.number} checks failed: {names}")
                if self.merge(pr.number):
                    return "merged"
            time.sleep(interval_s)
        raise RuntimeError(f"PR #{pr.number} did not merge within {timeout_s}s")


def commit_stage_list(repo: Path) -> list[str]:
    """Only code patches and run artifacts may ride the auto-PR. Nothing else."""
    r = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "runs", "tests"], cwd=repo,
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, check=True,
    )
    files: list[str] = []
    for line in r.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if path.startswith(COMMIT_STAGE_PREFIXES):
            files.append(path)
        elif path:
            raise RuntimeError(f"refusing auto-commit with unexpected change: {path}")
    return files


def commit_and_push(repo: str | Path, branch: str, message: str, token: str) -> list[str]:
    from .revise import require_main_branch, require_revision_trust

    require_revision_trust()
    require_main_branch(repo)
    repo = Path(repo)
    files = commit_stage_list(repo)
    if not files:
        raise RuntimeError("nothing to commit")
    base = ["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]
    subprocess.run(base + ["checkout", "-b", branch], cwd=repo, capture_output=True, text=True, timeout=60, check=True)
    subprocess.run(base + ["add", "--"] + files, cwd=repo, capture_output=True, text=True, timeout=60, check=True)
    subprocess.run(base + ["commit", "-m", message], cwd=repo, capture_output=True, text=True, timeout=60, check=True)
    push = subprocess.run(
        ["git", "-c", f"http.extraheader=AUTHORIZATION: bearer {token}", "push", "-u", "origin", branch],
        cwd=repo, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
    )
    if push.returncode != 0:
        raise RuntimeError(f"push failed: {push.stderr.strip()[-300:]}")
    return files
