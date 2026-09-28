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
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .journal import Journal, emit
from .muse_client import RequestBlocked
from .assess import AssessmentError, Proposal, AssessmentDoc, assess, assess_journal
from .changeset import (FORBIDDEN_FILES, FORBIDDEN_NAMES, FORBIDDEN_PREFIXES,
                        ChangesetError, check_policy, parse_unified_diff,
                        target_allowed, verify_in_disposable, worktree_status_paths)
from .memory import Memory
from . import promote as promotemod
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


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def tree_clean(repo: Path) -> bool:
    return git(repo, "status", "--porcelain", "--", "src", "tests").strip() == ""


def checks_pass(repo: Path, check_cmd: list[str], journal: Journal | None = None,
                timeout_s: int = 600) -> bool:
    try:
        r = subprocess.run(check_cmd, cwd=repo, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        emit(journal, "revise", "checks", f"cmd={' '.join(check_cmd)} TIMEOUT after {timeout_s}s")
        return False
    emit(journal, "revise", "checks", f"cmd={' '.join(check_cmd)} rc={r.returncode} tail={r.stdout[-300:] + r.stderr[-300:]}")
    return r.returncode == 0


def refuse_maintainer_credentials() -> None:
    """The revision worker must never hold export credentials. Fail closed."""
    import os

    if os.environ.get("GITHUB_TOKEN"):
        raise ContainmentBlocked("GITHUB_TOKEN must not enter the revision worker; "
                                 "export via a separate `canary publish` process")


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


def _outcome_manifest(cand) -> dict:
    """PatchOutcome-shaped manifest from a promotion candidate record."""
    base_files = cand.manifest.get("base_files", {})
    new_hashes = cand.manifest.get("new_hashes", {})
    files = []
    for entry in cand.manifest.get("files", []):
        path = entry["path"]
        files.append({"path": path, "op": entry.get("op", "modified"),
                      "old_sha": base_files.get(path, "absent"),
                      "new_sha": new_hashes.get(path, "absent")})
    return {"base_rev": cand.base_rev, "diff_sha": cand.diff_sha, "files": files}


def apply_one(repo: Path, proposal: Proposal, diff: str, check_cmd: list[str], journal: Journal | None,
              assessment_id: str = "",
              assess_dir: str | Path | None = None,
              context_record: dict | None = None) -> PatchOutcome:
    """Evaluate transactionally, then sync the live tree to the new accepted rev.

    Validation, application, and testing happen in a disposable copy of the
    latest accepted revision. Only the exact passing candidate promotes; the
    live tree is then synced to accepted (and must match it afterwards).
    Failed candidates never touch the live tree or the accepted revision.
    """
    repo = Path(repo)
    store = promotemod.Store(repo / "runs" / "promotions")
    if store.latest_rev() is None:
        if not tree_clean(repo):
            return PatchOutcome(proposal.id, proposal.target, False, False,
                                "worktree not clean; cannot anchor revision zero",
                                assessment_id=assessment_id)
        store.init_from_worktree(repo)

    def _record(status: str, reason: str, manifest: dict | None) -> None:
        if assess_dir is not None:
            save_candidate(assess_dir, proposal, diff, manifest, context_record,
                           assessment_id, status, reason)

    try:
        cand = promotemod.evaluate(store, proposal, diff, check_cmd, assessment_id,
                                   journal)
    except promotemod.PromotionHalt as e:
        raise RuntimeError(f"promotion store halted: {e}") from e
    manifest = _outcome_manifest(cand)
    if cand.state != "testing" or cand.test.get("exit") != 0:
        applied = any(t["state"] in ("applied", "testing") for t in cand.transitions)
        status = "rejected"
        _record(status, cand.reason_detail, manifest)
        return PatchOutcome(proposal.id, proposal.target, applied, False,
                            cand.reason_detail, cand.base_rev, manifest, assessment_id)
    try:
        rev_id = promotemod.promote(store, cand, journal)
    except promotemod.PromotionError as e:
        _record("rejected", str(e), manifest)
        return PatchOutcome(proposal.id, proposal.target, True, False, str(e),
                            cand.base_rev, manifest, assessment_id)
    emit(journal, "revise", "applied", f"{proposal.id}: rev {rev_id}")
    sync_worktree_to_accepted(repo, store, journal)
    _record("kept", f"checks green, kept as {rev_id}", manifest)
    return PatchOutcome(proposal.id, proposal.target, True, True,
                        f"checks green, kept as {rev_id}",
                        cand.base_rev, manifest, assessment_id)


def sync_worktree_to_accepted(repo: Path, store: "promotemod.Store",
                              journal: Journal | None = None) -> None:
    """Bring the live tree to the latest accepted snapshot. Halts on drift."""
    repo = Path(repo)
    rev_id = store.latest_rev()
    manifest = store.rev_manifest(rev_id) if rev_id else None
    if rev_id is None or manifest is None:
        raise RuntimeError("promotion store has no accepted revision to sync")
    live = promotemod.hash_tree(repo)
    want = manifest["files"]
    for path in sorted(set(live) ^ set(want)):
        target = repo / path
        if path in want:  # missing locally: restore from the snapshot
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(store.revs / rev_id / "tree" / path, target)
        else:  # extra locally: only accepted-side files may appear, never delete
            raise RuntimeError(f"worktree has unaccepted file {path}; reconcile manually")
    for path in sorted(set(live) & set(want)):
        if live[path] != want[path]:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(store.revs / rev_id / "tree" / path, target)
    ok, divergent = promotemod.worktree_matches(store, repo)
    if not ok:
        raise RuntimeError(f"worktree sync failed, divergent: {divergent}")
    emit(journal, "revise", "synced", f"worktree matches {rev_id}")


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
    refuse_maintainer_credentials()
    require_revision_trust()
    store = promotemod.Store(repo / "runs" / "promotions")
    if store.latest_rev() is None:
        if not tree_clean(repo):
            emit(journal, "revise", "aborted", "tree not clean")
            report.skipped = len(doc.proposals)
            return report
        store.init_from_worktree(repo)
    else:
        ok, divergent = promotemod.worktree_matches(store, repo)
        if not ok:
            emit(journal, "revise", "aborted",
                 f"worktree differs from {store.latest_rev()}: {divergent}")
            report.skipped = len(doc.proposals)
            return report
    if not checks_pass(repo, check_cmd, journal):
        emit(journal, "revise", "aborted", "baseline checks not green")
        report.skipped = len(doc.proposals)
        return report
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
        oc = apply_one(repo, proposal, diff, check_cmd, journal,
                       assessment_id, assess_dir, context_record)
        report.outcomes.append(oc)
        if oc.kept:
            report.kept += 1
        elif oc.applied:
            report.reverted += 1
        else:
            report.skipped += 1
    save_round(store, doc, report, assessment_id, journal)
    return report


def save_round(store: "promotemod.Store", doc: AssessmentDoc, rep: ReviseReport,
               assessment_id: str, journal: Journal | None = None) -> str:
    """Persist the round for the separate maintainer publish step."""
    import json

    rounds = store.root / "rounds"
    rounds.mkdir(parents=True, exist_ok=True)
    round_id = new_run_id()
    payload = {
        "round_id": round_id, "assessment_id": assessment_id,
        "accepted": store.latest_rev(),
        "doc": {"markdown": doc.markdown,
                "proposals": [asdict(p) for p in doc.proposals]},
        "rep": {"kept": rep.kept, "reverted": rep.reverted, "skipped": rep.skipped,
                "outcomes": [asdict(o) for o in rep.outcomes]},
    }
    (rounds / f"{round_id}.json").write_text(json.dumps(payload, indent=2) + "\n",
                                             encoding="utf-8")
    (rounds / "latest.json").write_text(json.dumps(payload, indent=2) + "\n",
                                        encoding="utf-8")
    emit(journal, "revise", "round-saved", round_id)
    return round_id


def load_round(store: "promotemod.Store", round_id: str = "latest") -> tuple[AssessmentDoc, ReviseReport]:
    """Rebuild a recorded round for `canary publish`. No execution involved."""
    import json

    payload = json.loads((store.root / "rounds" / f"{round_id}.json").read_text(encoding="utf-8"))
    doc = AssessmentDoc(markdown=payload["doc"]["markdown"],
                        proposals=tuple(Proposal(**p) for p in payload["doc"]["proposals"]))
    rep = ReviseReport(kept=payload["rep"]["kept"], reverted=payload["rep"]["reverted"],
                       skipped=payload["rep"]["skipped"],
                       outcomes=[PatchOutcome(**{k: o[k] for k in PatchOutcome.__dataclass_fields__
                                                  if k in o})
                                 for o in payload["rep"]["outcomes"]])
    return doc, rep


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

    refuse_maintainer_credentials()  # no export credentials in the worker
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
                                    code_revision=code_rev, tree=repo)
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
