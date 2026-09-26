"""Improve: apply self-proposed patches only if the test gate stays green.

Safety properties:
- tests/ are immutable: the agent may never weaken its own exam.
- .github/, Dockerfile*, lockfiles, and this gate file are off-limits.
- The tree must be clean and the suite green BEFORE any patch is attempted.
- Every patch is reverted unless the full suite passes after it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .journal import Journal, emit
from .reflect import Proposal, ReflectionDoc, reflect
from .synthesize import Completer

DIFF_SYSTEM = (
    "You emit ONLY a unified diff (git format with a/ b/ paths, no fences, no "
    "commentary) implementing the requested change. Keep it minimal and complete; "
    "the patch must apply with git apply and keep the test suite passing."
)

FORBIDDEN_PREFIXES = ("tests/", ".github/")
FORBIDDEN_NAMES = ("Dockerfile", ".dockerignore")
FORBIDDEN_FILES = ("src/autoresearch/improve.py",)
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
class ImproveReport:
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
    if not t.startswith("src/autoresearch/"):
        return "only src/autoresearch/ may be modified"
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
    emit(journal, "improve", "checks", f"cmd={' '.join(check_cmd)} rc={r.returncode} tail={r.stdout[-300:] + r.stderr[-300:]}")
    return r.returncode == 0


def request_diff(proposal: Proposal, client: Completer) -> str:
    user = f"Target file: {proposal.target}\nRequested change: {proposal.change}\nReason: {proposal.reason}\n\nEmit the diff."
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
            emit(journal, "improve", "rejected", f"{proposal.id}: {t} forbidden ({reason})")
            return PatchOutcome(proposal.id, t, False, False, f"forbidden target {t}: {reason}")
    ap = subprocess.run(["git", "apply", "-"], input=diff, cwd=repo, capture_output=True, text=True, timeout=60)
    if ap.returncode != 0:
        return PatchOutcome(proposal.id, proposal.target, False, False, f"git apply failed: {ap.stderr.strip()[:200]}")
    emit(journal, "improve", "applied", f"{proposal.id}: {', '.join(targets)}")
    if checks_pass(repo, check_cmd, journal):
        return PatchOutcome(proposal.id, proposal.target, True, True, "checks green, kept")
    # revert: restore tracked files, delete files the patch added
    for t in targets:
        p = repo / t
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", t], cwd=repo, capture_output=True).returncode == 0
        if tracked:
            subprocess.run(["git", "checkout", "--", t], cwd=repo, capture_output=True)
        elif p.exists():
            p.unlink()
    emit(journal, "improve", "reverted", f"{proposal.id}: checks failed")
    return PatchOutcome(proposal.id, proposal.target, True, False, "checks failed, reverted")


def improve_round(
    repo: str | Path,
    doc: ReflectionDoc,
    client: Completer,
    journal: Journal | None = None,
    check_cmd: list[str] | None = None,
) -> ImproveReport:
    repo = Path(repo)
    check_cmd = check_cmd or ["pytest", "-q"]
    report = ImproveReport()
    if not tree_clean(repo):
        emit(journal, "improve", "aborted", "tree not clean")
        report.skipped = len(doc.proposals)
        return report
    if not checks_pass(repo, check_cmd, journal):
        emit(journal, "improve", "aborted", "baseline checks not green")
        report.skipped = len(doc.proposals)
        return report
    for proposal in doc.proposals[:MAX_PROPOSALS_PER_ROUND]:
        try:
            diff = request_diff(proposal, client)
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


def improve_from_journal(
    journal_text: str,
    outcome: str,
    repo: str | Path,
    client: Completer,
    rounds: int = 1,
    check_cmd: list[str] | None = None,
    journal: Journal | None = None,
) -> tuple[ReflectionDoc, ImproveReport]:
    """Reflect on notes, then do the improvements. Returns last reflection + totals."""
    total = ImproveReport()
    doc = ReflectionDoc(markdown="", proposals=())
    for _ in range(max(rounds, 1)):
        doc = reflect(journal_text, outcome, client)
        emit(journal, "improve", "reflected", f"{len(doc.proposals)} proposals")
        rep = improve_round(repo, doc, client, journal, check_cmd)
        total.kept += rep.kept
        total.reverted += rep.reverted
        total.skipped += rep.skipped
        total.outcomes.extend(rep.outcomes)
        if rep.kept == 0:
            break
    return doc, total
