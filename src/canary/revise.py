"""Revise: apply proposed patches only if the test gate stays green.

Safety properties:
- tests/ are immutable: the worker may never weaken its own exam.
- .github/, Dockerfile*, lockfiles, and this gate file are off-limits.
- The tree must be clean and the suite green BEFORE any patch is attempted.
- Every patch is reverted unless the full suite passes after it.
- Until a trusted execution path exists (Builds 03-04), every public revision
  entry point refuses before any model call, subprocess, or filesystem
  mutation. See docs/TRUST_BOUNDARY.md.

This gate is an interlock against accidental uncontained execution, not a
security boundary against malicious in-process code: in-process callers can
always monkeypatch it, which is why the container boundary (Builds 03-05) is
the real enforcement. Tests exercise revision logic through that same
explicit monkeypatch seam; nothing implicit (environment, /.dockerenv,
container-mode strings) ever authorizes revision.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .journal import Journal, emit
from .muse_client import RequestBlocked
from .assess import AssessmentError, Proposal, AssessmentDoc, assess, assess_journal
from .changeset import (ChangesetError, check_policy, parse_unified_diff,
                        verify_in_disposable, worktree_status_paths)
from .memory import Memory
from .synthesize import Completer


class ContainmentBlocked(RuntimeError):
    """Revision refused: no trusted execution path. Fail closed, never guess."""


def require_revision_trust() -> None:
    """Fail closed: no caller-controlled evidence authorizes revision.

    Intentionally reads no environment, filesystem, or string flags. A
    trusted execution path (Builds 03-04) will replace this stub with a
    launcher-attested check; until then every public revision entry point
    refuses. In-process tests bypass it only via explicit monkeypatch.
    """
    raise ContainmentBlocked(
        "refusing: revision has no trusted execution path yet "
        "(see docs/TRUST_BOUNDARY.md, Builds 03-04); "
        "environment flags and container evidence are not authorization"
    )

DIFF_SYSTEM = (
    "You emit ONLY a unified diff (git format with a/ b/ paths, no fences, no "
    "commentary) implementing the requested change. Keep it minimal and complete; "
    "the patch must apply with git apply and keep the test suite passing."
)

FORBIDDEN_PREFIXES = ("tests/", ".github/", "boundary/", "scripts/", "recovery/",
                        "validation/", "evalpack/")
FORBIDDEN_NAMES = ("Dockerfile", ".dockerignore")
FORBIDDEN_FILES = ("src/canary/revise.py", "src/canary/muse_client.py",
                   "src/canary/changeset.py", "pyproject.toml",
                   "docs/TRUST_BOUNDARY.md", "docs/TOKEN_BOUNDARY.md")
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
    base_rev: str = ""
    manifest: dict | None = None
    assessment_id: str = ""


@dataclass
class ReviseReport:
    kept: int = 0
    reverted: int = 0
    skipped: int = 0
    outcomes: list[PatchOutcome] = field(default_factory=list)


def diff_targets(diff: str) -> list[str]:
    return re.findall(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE)


def target_allowed(target: str) -> str | None:
    """None if allowed, else the reason it is forbidden.

    The durable promotion policy: tests, evaluation, launcher, broker,
    credentials, workflows, lockfiles, recovery evidence, and the gate itself
    are outside worker control. Only src/canary/ worker code may be patched.
    """
    t = target.strip()
    if not t or t.startswith("/") or ".." in Path(t).parts:
        return "absolute or escaping path"
    if t.startswith(FORBIDDEN_PREFIXES):
        return "protected area (tests, workflows, launcher, broker, gate, evidence)"
    name = Path(t).name
    if name in FORBIDDEN_NAMES or t in FORBIDDEN_FILES:
        return "protected file"
    if name == ".env" or name.startswith(".env."):
        return "credentials are immutable"
    if t.endswith((".lock", ".pem", ".key")):
        return "lockfiles and keys are immutable"
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


def request_diff(proposal: Proposal, client: Completer, repo: Path | None = None,
                 record: dict | None = None) -> str:
    """Ask for a diff against real base context; record what was sent/omitted."""
    from .redact import redact_text

    base_rev = "unknown"
    file_sha = "absent"
    omitted = ""
    context = "(target content unavailable)"
    if repo is not None:
        repo = Path(repo)
        try:
            base_rev = git(repo, "rev-parse", "HEAD").strip()
        except Exception:
            base_rev = "unknown"
        p = repo / proposal.target
        try:
            raw = p.read_bytes() if p.exists() else None
            if raw is None:
                context = "(file does not exist yet)"
            else:
                file_sha = hashlib.sha256(raw).hexdigest()
                text = raw.decode("utf-8", "replace")
                if len(text) > 8000:
                    omitted = f"truncated to 8000 of {len(text)} chars"
                    text = text[:8000]
                context = text
        except OSError:
            context = "(target unreadable)"
            omitted = "target unreadable"
    redacted = redact_text(context)
    if record is not None:
        record.update({"base_rev": base_rev, "file_sha": file_sha,
                       "bytes_included": len(redacted),
                       "redacted": redacted != context, "omitted": omitted})
    user = (
        f"Target file: {proposal.target}\nRequested change: {proposal.change}\nReason: {proposal.reason}\n\n"
        f"Base revision: {base_rev}\nFile sha256: {file_sha}\n"
        + (f"Omitted context: {omitted}\n" if omitted else "") +
        f"Current content of {proposal.target}:\n```\n{redacted}\n```\n\nEmit the diff."
    )
    return client.complete(DIFF_SYSTEM, user)


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"


def save_candidate(assess_dir: str | Path, proposal: Proposal, diff: str,
                   manifest: dict | None, context_record: dict | None,
                   assessment_id: str, status: str, reason: str) -> Path:
    """Provenance for one candidate: assessment, snapshot, benefit, raw diff.

    The raw diff is redacted before persistence; secrets never land in records.
    """
    import json

    from .redact import redact_text

    out = Path(assess_dir) / "candidates"
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "proposal_id": proposal.id, "target": proposal.target, "change": proposal.change,
        "reason": proposal.reason, "assessment_id": assessment_id, "status": status,
        "detail": reason, "manifest": manifest,
        "context": context_record or {},
        "raw_diff": redact_text(diff),
    }
    path = out / f"{proposal.id}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def apply_one(repo: Path, proposal: Proposal, diff: str, check_cmd: list[str], journal: Journal | None,
              prior_diffs: tuple[str, ...] = (), assessment_id: str = "",
              assess_dir: str | Path | None = None,
              context_record: dict | None = None) -> PatchOutcome:
    """Validate structurally, validate in a disposable checkout, then apply.

    The candidate binds to an exact base revision and file hashes; any drift
    between validation and evaluation rejects without editing. Post-apply, the
    tree must contain exactly the manifest files (plus caches) or it reverts.
    """
    repo = Path(repo)

    def _record(status: str, reason: str, manifest: dict | None) -> None:
        if assess_dir is not None:
            save_candidate(assess_dir, proposal, diff, manifest, context_record,
                           assessment_id, status, reason)

    try:
        ops = parse_unified_diff(diff)
        check_policy(ops, target_allowed)
    except ChangesetError as e:
        emit(journal, "revise", "rejected", f"{proposal.id}: {e}")
        _record("rejected", str(e), None)
        return PatchOutcome(proposal.id, proposal.target, False, False, str(e),
                            assessment_id=assessment_id)
    try:
        manifest = verify_in_disposable(repo, diff, ops, prior_diffs)
    except ChangesetError as e:
        reason = f"disposable validation failed: {e}"
        emit(journal, "revise", "rejected", f"{proposal.id}: {reason}"[:300])
        _record("rejected", reason, None)
        return PatchOutcome(proposal.id, proposal.target, False, False, reason,
                            assessment_id=assessment_id)
    if git(repo, "rev-parse", "HEAD").strip() != manifest.base_rev:
        reason = "base revision moved between validation and evaluation"
        _record("rejected", reason, manifest.to_dict())
        return PatchOutcome(proposal.id, proposal.target, False, False, reason,
                            manifest.base_rev, manifest.to_dict(), assessment_id)
    for entry in manifest.files:
        if _file_sha(repo / entry["path"]) != entry["old_sha"]:
            reason = (f"source mutation: {entry['path']} changed between validation "
                      "and evaluation")
            _record("rejected", reason, manifest.to_dict())
            return PatchOutcome(proposal.id, proposal.target, False, False, reason,
                                manifest.base_rev, manifest.to_dict(), assessment_id)
    targets = [entry["path"] for entry in manifest.files]
    snapshot: dict[str, bytes | None] = {}
    for t in targets:
        p = repo / t
        snapshot[t] = p.read_bytes() if p.exists() else None
    ap = subprocess.run(["git", "apply", "-"], input=diff, cwd=repo, capture_output=True,
                        text=True, timeout=60)
    if ap.returncode != 0:
        reason = f"git apply failed: {ap.stderr.strip()[:200]}"
        _record("rejected", reason, manifest.to_dict())
        return PatchOutcome(proposal.id, proposal.target, False, False, reason,
                            manifest.base_rev, manifest.to_dict(), assessment_id)

    def _restore() -> None:
        for t, data in snapshot.items():
            p = repo / t
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                p.write_bytes(data)

    for entry in manifest.files:  # the apply must produce exactly the manifest
        if _file_sha(repo / entry["path"]) != entry["new_sha"]:
            _restore()
            reason = f"post-apply state mismatch on {entry['path']}"
            _record("rejected", reason, manifest.to_dict())
            return PatchOutcome(proposal.id, proposal.target, False, False, reason,
                                manifest.base_rev, manifest.to_dict(), assessment_id)
    emit(journal, "revise", "applied", f"{proposal.id}: {', '.join(targets)}")
    if not checks_pass(repo, check_cmd, journal):
        _restore()
        emit(journal, "revise", "reverted", f"{proposal.id}: checks failed")
        _record("reverted", "checks failed, reverted", manifest.to_dict())
        return PatchOutcome(proposal.id, proposal.target, True, False,
                            "checks failed, reverted", manifest.base_rev, manifest.to_dict(),
                            assessment_id)
    extras = {p for p in worktree_status_paths(repo, "src", "tests") if "__pycache__" not in p
              and not p.endswith(".pyc")} - set(targets)
    if extras:
        _restore()
        reason = f"unreported files changed: {sorted(extras)[:5]}"
        emit(journal, "revise", "reverted", f"{proposal.id}: {reason}")
        _record("reverted", reason, manifest.to_dict())
        return PatchOutcome(proposal.id, proposal.target, True, False, reason,
                            manifest.base_rev, manifest.to_dict(), assessment_id)
    _record("kept", "checks green, kept", manifest.to_dict())
    return PatchOutcome(proposal.id, proposal.target, True, True, "checks green, kept",
                        manifest.base_rev, manifest.to_dict(), assessment_id)


def revise_round(
    repo: str | Path,
    doc: AssessmentDoc,
    client: Completer,
    journal: Journal | None = None,
    check_cmd: list[str] | None = None,
    assess_dir: str | Path | None = None,
    assessment_id: str = "",
) -> ReviseReport:
    repo = Path(repo)
    check_cmd = check_cmd or ["pytest", "-q"]
    report = ReviseReport()
    require_revision_trust()
    if not tree_clean(repo):
        emit(journal, "revise", "aborted", "tree not clean")
        report.skipped = len(doc.proposals)
        return report
    if not checks_pass(repo, check_cmd, journal):
        emit(journal, "revise", "aborted", "baseline checks not green")
        report.skipped = len(doc.proposals)
        return report
    prior_diffs: list[str] = []
    for proposal in doc.proposals[:MAX_PROPOSALS_PER_ROUND]:
        context_record: dict = {}
        try:
            diff = request_diff(proposal, client, repo, context_record)
        except RequestBlocked:
            raise
        except Exception as e:
            report.outcomes.append(PatchOutcome(proposal.id, proposal.target, False, False, f"diff request failed: {e}"))
            report.skipped += 1
            continue
        oc = apply_one(repo, proposal, diff, check_cmd, journal, tuple(prior_diffs),
                       assessment_id, assess_dir, context_record)
        report.outcomes.append(oc)
        if oc.kept:
            report.kept += 1
            prior_diffs.append(diff)
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
    assess_dir: str | Path | None = None,
    budget=None,
) -> tuple[AssessmentDoc, ReviseReport]:
    """Assess notes, then do the revisions. Returns last assessment + totals.

    Every round persists assessment records and files patch outcomes as
    lessons. Later rounds see fresh notes (including prior round outcomes)
    and the current tree — never the stale first-round input.
    """
    from .report import code_revision

    require_revision_trust()  # before the assess model call, not after it
    repo = Path(repo)
    adir = Path(assess_dir) if assess_dir else repo / "runs" / "assessments"
    memory = Memory(adir / "memory.jsonl")
    code_rev = code_revision()
    total = ReviseReport()
    doc = AssessmentDoc(markdown="", proposals=())
    current_text = journal_text
    prior: list[str] = []
    for rnd in range(max(rounds, 1)):
        if rnd > 0 and journal is not None:
            current_text = journal.text(max_chars=60000)  # fresh: prior rounds included
        notes = journal.notes if journal is not None else None
        round_outcome = outcome if not prior else (
            f"{outcome}\nPrior patch outcomes:\n" + "\n".join(prior))
        try:
            record = assess_journal(notes, current_text, round_outcome, client,
                                    memory=memory, budget=budget, out_dir=adir,
                                    code_revision=code_rev)
        except AssessmentError as e:
            emit(journal, "revise", "assess-failed", str(e)[:200])
            break
        doc = record.doc
        emit(journal, "revise", "assessed",
             f"{record.id}: {len(doc.proposals)} proposals, coverage={record.status}")
        rep = revise_round(repo, doc, client, journal, check_cmd, adir, record.id)
        for oc in rep.outcomes:
            memory.record(text=f"{oc.proposal_id} {oc.target}: {oc.reason}",
                          target=oc.target, kept=oc.kept, assessment_id=record.id,
                          code_revision=code_rev)
            prior.append(f"{oc.proposal_id} {oc.target}: "
                         f"{'kept' if oc.kept else 'reverted' if oc.applied else 'skipped'}"
                         f" ({oc.reason})")
        memory.save()
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

    require_revision_trust()  # before runs/ writes and any GitHub side effects
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
        except RequestBlocked:
            raise
        except Exception:
            pass
    except RequestBlocked:
        raise
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
