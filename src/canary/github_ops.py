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


def run_git(
    args: list[str],
    cwd: str | Path | None = None,
    timeout: float = 30,
    check: bool = False,
    stdin=None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command without leaving zombie child processes.

    Uses ``Popen`` as a context-manager plus ``communicate(timeout=...)``
    with an explicit ``wait()`` in a ``finally`` block so the child is
    always reaped, even on timeouts or other errors.
    """
    if stdin is None:
        stdin = subprocess.DEVNULL
    with subprocess.Popen(
        args,
        cwd=cwd,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            raise
        except BaseException:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise
        finally:
            try:
                proc.wait()
            except Exception:
                pass
        rc = proc.returncode
    completed = subprocess.CompletedProcess(args, rc, stdout, stderr)
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, args, output=stdout, stderr=stderr)
    return completed


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
        r = run_git(["git", "remote", "get-url", "origin"], cwd=repo_root, timeout=30)
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

    def reviews(self, number: int) -> list[dict]:
        d = self._get(f"/repos/{self.repo}/pulls/{number}/reviews")
        return d if isinstance(d, list) else []

    def independently_approved(self, number: int, author: str | None) -> bool:
        """An APPROVED review from anyone but the PR author (Issue #62).

        Same-identity approval is no check against intentional evasion, so
        it never counts. Unknown author fails closed. Which separate
        identity must approve is pinned owner-side by branch ruleset; this
        gate guarantees independence, not identity.
        """
        if not author:
            return False
        try:
            reviews = self.reviews(number)
        except Exception:
            return False
        return any(r.get("state") == "APPROVED"
                   and isinstance(r.get("user"), dict)
                   and r["user"].get("login") not in (None, "", author)
                   for r in reviews if isinstance(r, dict))

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
        """Enable auto-merge, then ensure merge happens. Returns 'merged' or raises.

        Green checks alone never merge: an independent (non-author) approval
        must also be present, else we keep waiting for one until the timeout.
        """
        self.enable_automerge(pr.node_id)
        deadline = time.monotonic() + timeout_s
        unapproved = False
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
                author = state.get("user") or {}
                if not self.independently_approved(pr.number, author.get("login")):
                    unapproved = True
                    time.sleep(interval_s)
                    continue
                if self.merge(pr.number):
                    return "merged"
            time.sleep(interval_s)
        if unapproved:
            raise RuntimeError(
                f"PR #{pr.number} has no independent approval (self-approval does not count)")
        raise RuntimeError(f"PR #{pr.number} did not merge within {timeout_s}s")


def commit_stage_list(repo: Path) -> list[str]:
    """Only code patches and run artifacts may ride the auto-PR. Nothing else."""
    r = run_git(
        ["git", "status", "--porcelain", "--", "src", "runs", "tests"],
        cwd=repo,
        timeout=30,
        check=True,
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
    run_git(base + ["checkout", "-b", branch], cwd=repo, timeout=60, check=True)
    run_git(base + ["add", "--"] + files, cwd=repo, timeout=60, check=True)
    run_git(base + ["commit", "-m", message], cwd=repo, timeout=60, check=True)
    push = run_git(
        ["git", "-c", f"http.extraheader=AUTHORIZATION: bearer {token}", "push", "-u", "origin", branch],
        cwd=repo,
        timeout=300,
    )
    if push.returncode != 0:
        raise RuntimeError(f"push failed: {push.stderr.strip()[-300:]}")
    return files
