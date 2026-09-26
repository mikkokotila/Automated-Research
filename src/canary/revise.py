"""Revise: apply proposed patches only if the test gate stays green.

Safety properties:
- tests/ are immutable: the worker may never weaken its own exam.
- .github/, Dockerfile*, lockfiles, and this gate file are off-limits.
- The tree must be clean and the suite green BEFORE any patch is attempted.
- Every patch is reverted unless the full suite passes after it.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .journal import Journal, emit
from .assess import Proposal, AssessmentDoc, assess
from .synthesize import Completer


def require_sandbox() -> None:
    """Revision runs only in the sandbox container. Enforced, not advised."""
    if os.environ.get("CANARY_SANDBOXED") == "1" or Path("/.dockerenv").exists():
        return
    raise RuntimeError(
        "refusing: revision runs only inside the sandbox container "
        "(see scripts/container_run.sh); CANARY_SANDBOXED=1 not set"
    )

DIFF_SYSTEM = (
    "You emit ONLY a unified diff (git format with a/ b/ paths, no fences, no "
    "commentary) implementing the requested change. Keep it minimal and complete; "
    "the patch must apply with git apply and keep the test suite passing."
)

FORBIDDEN_PREFIXES = ("tests/", ".github/")
FORBIDDEN_NAMES = ("Dockerfile", ".dockerignore")
FORBIDDEN_FILES = ("src/canary/revise.py",)
MAX_DIFF_FILES = 5
MAX_DIFF_LINES = 300
MAX_PROPOSALS_PER_ROUND = 3


@dataclass
class PatchOutcome:
    proposal_id: str
    target: str
    applied: bool
    kept: bool
    reason: str


@dataclass
class ReviseReport:
    kept: int = 0
    reverted: int = 0
    skipped: int = 0
    outcomes: list[PatchOutcome] = field(default_factory=list)


def diff_targets(diff: str) -> list[str]:
    return re.findall(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE)


def target_allowed(target: str) -> str | None:
    """None if allowed, else the reason it is forbidden."""
    t = target.strip()
    if not t or t.startswith("/") or ".." in Path(t).parts:
        return "absolute or escaping path"
    if t.startswith(FORBIDDEN_PREFIXES):
        return "protected area (tests and workflows are immutable)"
    if Path(t).name in FORBIDDEN_NAMES or t in FORBIDDEN_FILES:
        return "protected file"
    if t.endswith(".lock"):
        return "lockfiles are immutable"
    if not t.startswith("src/canary/"):
        return "only src/canary/ may be modified"
    return None


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def tree_clean(repo: Path) -> bool:
    return git(repo, "status", "--porcelain", "--", "src", "tests").strip() == ""


def checks_pass(repo: Path, check_cmd: list[str], journal: Journal | None = None) -> bool:
    r = subprocess.run(check_cmd, cwd=repo, capture_output=True, text=True, timeout=600)
    emit(journal, "revise", "checks", f"cmd={' '.join(check_cmd)} rc={r.returncode} tail={r.stdout[-300:] + r.stderr[-300:]}")
    return r.returncode == 0


def request_diff(proposal: Proposal, client: Completer, repo: Path | None = None) -> str:
    context = "(target content unavailable)"
    if repo is not None:
        p = repo / proposal.target
        try:
            context = p.read_text(encoding="utf-8")[:8000] if p.exists() else "(file does not exist yet)"
        except OSError:
            context = "(target unreadable)"
    user = (
        f"Target file: {proposal.target}\nRequested change: {proposal.change}\nReason: {proposal.reason}\n\n"
        f"Current content of {proposal.target}:\n```\n{context}\n```\n\nEmit the diff."
    )
    return client.complete(DIFF_SYSTEM, user)


def apply_one(repo: Path, proposal: Proposal, diff: str, check_cmd: list[str], journal: Journal | None) -> PatchOutcome:
    targets = diff_targets(diff)
    if not targets:
        return PatchOutcome(proposal.id, proposal.target, False, False, "diff has no file targets")
    if len(targets) > MAX_DIFF_FILES:
        return PatchOutcome(proposal.id, proposal.target, False, False, f"diff touches {len(targets)} files (cap {MAX_DIFF_FILES})")
    if len(diff.splitlines()) > MAX_DIFF_LINES:
        return PatchOutcome(proposal.id, proposal.target, False, False, f"diff too large (cap {MAX_DIFF_LINES} lines)")
    for t in targets:
        reason = target_allowed(t)
        if reason:
            emit(journal, "revise", "rejected", f"{proposal.id}: {t} forbidden ({reason})")
            return PatchOutcome(proposal.id, t, False, False, f"forbidden target {t}: {reason}")
    snapshot: dict[str, bytes | None] = {}
    for t in targets:
        p = repo / t
        snapshot[t] = p.read_bytes() if p.exists() else None
    ap = subprocess.run(["git", "apply", "-"], input=diff, cwd=repo, capture_output=True, text=True, timeout=60)
    if ap.returncode != 0:
        return PatchOutcome(proposal.id, proposal.target, False, False, f"git apply failed: {ap.stderr.strip()[:200]}")
    emit(journal, "revise", "applied", f"{proposal.id}: {', '.join(targets)}")
    if checks_pass(repo, check_cmd, journal):
        return PatchOutcome(proposal.id, proposal.target, True, True, "checks green, kept")
    for t, data in snapshot.items():  # restore pre-patch bytes, never HEAD (keeps earlier patches)
        p = repo / t
        if data is None:
            if p.exists():
                p.unlink()
        else:
            p.write_bytes(data)
    emit(journal, "revise", "reverted", f"{proposal.id}: checks failed")
    return PatchOutcome(proposal.id, proposal.target, True, False, "checks failed, reverted")


def revise_round(
    repo: str | Path,
    doc: AssessmentDoc,
    client: Completer,
    journal: Journal | None = None,
    check_cmd: list[str] | None = None,
) -> ReviseReport:
    repo = Path(repo)
    check_cmd = check_cmd or ["pytest", "-q"]
    report = ReviseReport()
    require_sandbox()
    if not tree_clean(repo):
        emit(journal, "revise", "aborted", "tree not clean")
        report.skipped = len(doc.proposals)
        return report
    if not checks_pass(repo, check_cmd, journal):
        emit(journal, "revise", "aborted", "baseline checks not green")
        report.skipped = len(doc.proposals)
        return report
    for proposal in doc.proposals[:MAX_PROPOSALS_PER_ROUND]:
        try:
            diff = request_diff(proposal, client, repo)
        except Exception as e:
            report.outcomes.append(PatchOutcome(proposal.id, proposal.target, False, False, f"diff request failed: {e}"))
            report.skipped += 1
            continue
        oc = apply_one(repo, proposal, diff, check_cmd, journal)
        report.outcomes.append(oc)
        if oc.kept:
            report.kept += 1
        elif oc.applied:
            report.reverted += 1
        else:
            report.skipped += 1
    return report


def revise_from_journal(
    journal_text: str,
    outcome: str,
    repo: str | Path,
    client: Completer,
    rounds: int = 1,
    check_cmd: list[str] | None = None,
    journal: Journal | None = None,
) -> tuple[AssessmentDoc, ReviseReport]:
    """Assess notes, then do the revisions. Returns last assessment + totals."""
    total = ReviseReport()
    doc = AssessmentDoc(markdown="", proposals=())
    for _ in range(max(rounds, 1)):
        doc = assess(journal_text, outcome, client)
        emit(journal, "revise", "assessed", f"{len(doc.proposals)} proposals")
        rep = revise_round(repo, doc, client, journal, check_cmd)
        total.kept += rep.kept
        total.reverted += rep.reverted
        total.skipped += rep.skipped
        total.outcomes.extend(rep.outcomes)
        if rep.kept == 0:
            break
    return doc, total


def new_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


@dataclass
class PublishReport:
    issue_number: int = 0
    issue_url: str = ""
    pr_url: str = ""
    merged: bool = False
    branch: str = ""


def publish_round(
    repo: str | Path,
    run_id: str,
    doc: AssessmentDoc,
    rep: ReviseReport,
    journal: Journal,
    gh,  # GitHub client (duck-typed for tests)
    merge_timeout_s: float = 900,
    merge_interval_s: float = 20,
) -> PublishReport:
    """File the round as an Issue; ship kept patches via auto-merged PR."""
    from .github_ops import commit_and_push

    repo = Path(repo)
    runs = repo / "runs"
    runs.mkdir(exist_ok=True)
    (runs / f"{run_id}-assessment.md").write_text(doc.markdown + "\n", encoding="utf-8")
    journal.save(runs / f"{run_id}-journal.jsonl")
    rows = [f"| {o.proposal_id} | {o.target} | {'kept' if o.kept else 'reverted' if o.applied else 'skipped'} | {o.reason} |"
            for o in rep.outcomes] or ["| — | — | no proposals | — |"]
    body = f"{doc.markdown}\n\n## Patches (kept={rep.kept} reverted={rep.reverted} skipped={rep.skipped})\n\n" + \
        "| id | target | result | reason |\n|---|---|---|---|\n" + "\n".join(rows)
    issue = gh.create_issue(f"Maintenance {run_id}", body)
    emit(journal, "publish", "issue", f"#{issue.number}")
    report = PublishReport(issue_number=issue.number, issue_url=issue.url)
    if rep.kept == 0:
        return report
    branch = f"auto/revise-{run_id}"
    files = commit_and_push(repo, branch, f"auto: maintenance {run_id}", _token())
    emit(journal, "publish", "pushed", f"{branch} ({len(files)} files)")
    pr = gh.create_pr(
        f"auto: maintenance {run_id}",
        f"Closes #{issue.number}\n\n{body}",
        head=branch,
    )
    report.pr_url = pr.url
    report.branch = branch
    emit(journal, "publish", "pr", pr.url)
    try:
        gh.wait_and_merge(pr, timeout_s=merge_timeout_s, interval_s=merge_interval_s)
        report.merged = True
        gh.comment(issue.number, f"Merged: {pr.url}")
        try:
            gh.delete_branch(branch)
        except Exception:
            pass
    except Exception as e:
        gh.comment(issue.number, f"Auto-merge failed, needs a human: {e}\n\nPR: {pr.url}")
        emit(journal, "publish", "merge-failed", str(e)[:200])
    return report


def _token() -> str:
    import os

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN required to push")
    return token
