"""Revise: apply proposed patches only if the test gate stays green.

Safety properties:
- tests/ are immutable: the worker may never weaken its own exam.
- .github/, Dockerfile*, lockfiles, and this gate file are off-limits.
- The tree must be clean and the suite green BEFORE any patch is attempted.
- Every patch is reverted unless the full suite passes after it.
- Until a launcher-attested execution path exists, every public revision
  entry point refuses before any model call, subprocess, or filesystem
  mutation. See docs/TRUST_BOUNDARY.md.

This gate is an interlock against accidental uncontained execution, not a
security boundary against malicious in-process code: in-process callers can
always monkeypatch it, which is why the container boundary (Builds 03-05) is
the real enforcement. The interim role check (Issue #52) authorizes exactly
two launcher/operator-provided markers; host-side attestation is deferred
(Issues #62-#64). Tests exercise revision logic through the explicit
monkeypatch seam plus genuine-path tests with the role markers set.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .journal import Journal, emit
from .muse_client import RequestBlocked
from .assess import AssessmentError, Proposal, AssessmentDoc, assess, assess_journal
from .changeset import (FORBIDDEN_FILES, FORBIDDEN_NAMES, FORBIDDEN_PREFIXES,
                        ChangesetError, check_policy, normalize_unified_diff,
                        parse_unified_diff,
                        target_allowed, verify_in_disposable, worktree_status_paths)
from .memory import Memory
from . import promote as promotemod
from .synthesize import Completer


class ContainmentBlocked(RuntimeError):
    """Revision refused: no trusted execution path. Fail closed, never guess."""


GUEST_ENV_VAR = "CANARY_GUEST"
PUBLISHER_ENV_VAR = "CANARY_PUBLISHER"
TRUSTED_ROLE_VALUE = "1"


def require_revision_trust() -> None:
    """Interim role check: exactly one launcher/operator role marker (Issue #52).

    CANARY_GUEST=1 is injected only by scripts/container_run.sh into
    disposable workers (which must hold no export credentials).
    CANARY_PUBLISHER=1 is set only by the owner's publish driver on the
    host (which holds GITHUB_TOKEN and never runs worker code).
    Anything else — including both markers at once — fails closed.

    INTERIM AND FORGEABLE: a marker is a claim, not proof. It stops
    accidental uncontained runs, not a malicious in-process actor (which
    can monkeypatch this anyway) or a forged guest. Host-side
    attestation (Issue #64), an independent review identity (#62), and
    externally governed CI (#63) are deferred hardening; until they
    land, treat this as a misconfiguration interlock, not a boundary.
    """
    import os

    guest = os.environ.get(GUEST_ENV_VAR) == TRUSTED_ROLE_VALUE
    publisher = os.environ.get(PUBLISHER_ENV_VAR) == TRUSTED_ROLE_VALUE
    if guest and publisher:
        raise ContainmentBlocked(
            "refusing: ambiguous revision role "
            "(guest and publisher markers are both set)")
    if not (guest or publisher):
        raise ContainmentBlocked(
            "refusing: revision needs a launcher-provided role "
            "(CANARY_GUEST=1 in a disposable worker, or CANARY_PUBLISHER=1 "
            "in the owner publish driver); refusing on this host"
        )


def current_branch(repo: Path) -> str:
    """Worktree branch, or ContainmentBlocked when git cannot say (fail closed)."""
    try:
        return git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except Exception as e:
        raise ContainmentBlocked(
            f"refusing: cannot determine worktree branch: {e}") from e


def require_prod_branch(repo: str | Path, profile: str) -> None:
    """Prod-profile runs execute on main only (code-level; see Issue #64).

    Dev runs may use any branch. Remote-SHA attestation (is this main the
    current origin/main?) cannot be proven where this check runs and is
    deferred to the host-side prod gate; until then this stops branch
    mistakes, not a forged checkout.
    """
    if profile not in ("dev", "prod"):
        raise ContainmentBlocked(f"refusing: unknown run profile {profile!r}")
    if profile == "dev":
        return
    branch = current_branch(Path(repo))
    if branch != "main":
        raise ContainmentBlocked(
            f"refusing: prod profile requires the main branch (on {branch!r})")


def require_main_branch(repo: str | Path) -> None:
    """Publish cuts auto-branches from main only, so PRs carry no foreign commits."""
    branch = current_branch(Path(repo))
    if branch != "main":
        raise ContainmentBlocked(
            f"refusing: publish requires the main branch (on {branch!r})")

DIFF_SYSTEM = (
    "You emit ONLY a unified diff (git format with a/ b/ paths, no fences, no "
    "commentary) implementing the requested change. Keep it minimal and complete; "
    "the patch must apply with git apply and keep the test suite passing."
)

MAX_DIFF_FILES = 5
MAX_DIFF_LINES = 300
MAX_PROPOSALS_PER_ROUND = 3
MAX_DIFF_ATTEMPTS = 2


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


def guest_pressure() -> str:
    """One-line pid/zombie/memory snapshot for gate forensics (Linux only).

    keep1h's post-run suite failed to fork with no static mechanism in our
    code; this reports the numbers so the next occurrence convicts with
    data instead of archaeology. Best-effort: "" anywhere it cannot read.
    """
    import os

    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return ""
    zombies = 0
    comms: dict[str, int] = {}
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                comm, rest = fh.read().split(") ", 1)
            if not rest.startswith("Z"):
                continue
            name = comm.split("(", 1)[1][:15]
        except (OSError, IndexError, ValueError):
            continue
        zombies += 1
        comms[name] = comms.get(name, 0) + 1
    ztop = ",".join(f"{name}:{count}"
                    for name, count in sorted(comms.items())[:5]) or "-"
    ceiling = "?"
    for candidate in ("/sys/fs/cgroup/pids.max",
                      "/sys/fs/cgroup/pids/pids.max"):
        try:
            ceiling = Path(candidate).read_text(encoding="utf-8").strip()
            break
        except OSError:
            continue
    mem_kb = "?"
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                mem_kb = line.split()[1]
                break
    except OSError:
        pass
    return (f" pids={len(pids)}/{ceiling} zombies={zombies} "
            f"zcomms={ztop} memavail_kb={mem_kb}")


def checks_pass(repo: Path, check_cmd: list[str], journal: Journal | None = None,
                timeout_s: int = 600, log_path: str | Path | None = None) -> bool:
    from .redact import redact_text

    before = guest_pressure()
    try:
        r = subprocess.run(check_cmd, cwd=repo, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        emit(journal, "revise", "checks", f"cmd={' '.join(check_cmd)} TIMEOUT after {timeout_s}s")
        return False
    after = guest_pressure()
    logged = ""
    if log_path is not None:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text(
                redact_text(f"$ {' '.join(check_cmd)}\nrc={r.returncode}\n"
                            f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"),
                encoding="utf-8")
            logged = f" log={Path(log_path).name}"
        except OSError:
            logged = " log=unwritable"  # best-effort: never fail the gate
    emit(journal, "revise", "checks", f"cmd={' '.join(check_cmd)} rc={r.returncode}{logged} before=[{before.strip()}] after=[{after.strip()}] tail={r.stdout[-300:] + r.stderr[-300:]}")
    return r.returncode == 0


def refuse_maintainer_credentials() -> None:
    """The revision worker must never hold export credentials. Fail closed."""
    import os

    if os.environ.get("GITHUB_TOKEN"):
        raise ContainmentBlocked("GITHUB_TOKEN must not enter the revision worker; "
                                 "export via a separate `canary publish` process")


def request_diff(proposal: Proposal, client: Completer, repo: Path | None = None,
                 record: dict | None = None, feedback: str = "") -> str:
    """Ask for a diff against real base context; record what was sent/omitted.

    Feedback carries a previous attempt's failure so the retry does not
    repeat it. Callers that retry must bound attempts (MAX_DIFF_ATTEMPTS).
    """
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
        + (f"Omitted context: {omitted}\n" if omitted else "")
        + (f"Previous attempt failed, do not repeat it: {feedback}\n" if feedback else "") +
        f"Current content of {proposal.target}:\n```\n{redacted}\n```\n\nEmit the diff."
    )
    return client.complete(DIFF_SYSTEM, user)


def request_applicable_diff(proposal: Proposal, client: Completer, repo: Path,
                            context_record: dict | None = None,
                            journal: Journal | None = None,
                            max_attempts: int = MAX_DIFF_ATTEMPTS,
                            attempts_out: list | None = None) -> str:
    """Request a diff that parses, passes policy, and applies to the base.

    Malformed or non-applying diffs get a retry with the failure fed back;
    without this, most model diffs die expensively in full eval (demo52c:
    1 applicable in 5). Raises ChangesetError when no attempt applies —
    the caller records a skip, never a silent drop. Every attempt lands in
    attempts_out (raw bytes, normalized bytes, error) so failed shapes stay
    diagnosable from the record instead of vanishing (keep1c).
    """
    feedback = ""
    for attempt in range(max(1, max_attempts)):
        raw = request_diff(proposal, client, repo, context_record, feedback=feedback)
        record = {"attempt": attempt + 1, "raw": raw, "normalized": "",
                  "error": ""}
        try:
            diff = normalize_unified_diff(raw)
            record["normalized"] = diff
            ops = parse_unified_diff(diff)
            check_policy(ops, target_allowed)
            verify_in_disposable(repo, diff, ops)
            if attempts_out is not None:
                attempts_out.append(record)
            return diff
        except ChangesetError as e:
            record["error"] = str(e)[:300]
            if attempts_out is not None:
                attempts_out.append(record)
            feedback = str(e)[:300]
            emit(journal, "revise", "diff-retry",
                 f"{proposal.id} attempt {attempt + 1}: {feedback}")
    raise ChangesetError(f"no applicable diff after {max(1, max_attempts)} attempts: {feedback}")


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"


def save_candidate(assess_dir: str | Path, proposal: Proposal, diff: str,
                   manifest: dict | None, context_record: dict | None,
                   assessment_id: str, status: str, reason: str,
                   attempts: list | None = None) -> Path:
    """Provenance for one candidate: assessment, snapshot, benefit, raw diff.

    The raw diff is redacted before persistence; secrets never land in records.
    Failed attempts persist alongside (redacted), so malformed model output
    stays diagnosable instead of vanishing with only an error one-liner.
    """
    import json

    from .redact import redact_text

    out = Path(assess_dir) / "candidates"
    out.mkdir(parents=True, exist_ok=True)
    kept_attempts = []
    for entry in attempts or []:
        raw_text, norm_text = (str(entry.get("raw", ""))[:20000],
                               str(entry.get("normalized", ""))[:20000])
        kept_attempts.append({
            "attempt": entry.get("attempt"),
            "error": entry.get("error", ""),
            "normalized": bool(norm_text) and norm_text != raw_text,
            "raw_diff": redact_text(raw_text),
            "normalized_diff": redact_text(norm_text),
        })
    payload = {
        "proposal_id": proposal.id, "target": proposal.target, "change": proposal.change,
        "reason": proposal.reason, "assessment_id": assessment_id, "status": status,
        "detail": reason, "manifest": manifest,
        "context": context_record or {},
        "raw_diff": redact_text(diff),
        "attempts": kept_attempts,
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


def _record_ignore(repo: Path, assess_dir: str | Path | None) -> tuple[str, ...]:
    """Top-level record dir to exclude from revision snapshots.

    Run outputs (journal, checkpoints, candidates) live under out_dir,
    which usually sits inside the repo under revision. Snapshots track
    code, not run records: without the exclusion, the run's own writes
    diverge the worktree and veto every later round (demo52b). Never
    excludes src/ or tests/; assess dirs outside the repo need nothing.
    """
    if assess_dir is None:
        return ()
    try:
        rel = Path(assess_dir).resolve().relative_to(Path(repo).resolve())
    except (OSError, ValueError):
        return ()
    if not rel.parts or rel.parts[0] in ("src", "tests"):
        return ()
    return (rel.parts[0],)


def apply_one(repo: Path, proposal: Proposal, diff: str, check_cmd: list[str], journal: Journal | None,
              assessment_id: str = "",
              assess_dir: str | Path | None = None,
              context_record: dict | None = None,
              attempts: list | None = None) -> PatchOutcome:
    """Evaluate transactionally, then sync the live tree to the new accepted rev.

    Validation, application, and testing happen in a disposable copy of the
    latest accepted revision. Only the exact passing candidate promotes; the
    live tree is then synced to accepted (and must match it afterwards).
    Failed candidates never touch the live tree or the accepted revision.
    """
    repo = Path(repo)
    store = promotemod.Store(repo / "runs" / "promotions")
    ignore = _record_ignore(repo, assess_dir)
    if store.latest_rev() is None:
        if not tree_clean(repo):
            return PatchOutcome(proposal.id, proposal.target, False, False,
                                "worktree not clean; cannot anchor revision zero",
                                assessment_id=assessment_id)
        store.init_from_worktree(repo, ignore)

    def _record(status: str, reason: str, manifest: dict | None) -> None:
        if assess_dir is not None:
            save_candidate(assess_dir, proposal, diff, manifest, context_record,
                           assessment_id, status, reason, attempts)

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
    sync_worktree_to_accepted(repo, store, journal, ignore)
    _record("kept", f"checks green, kept as {rev_id}", manifest)
    return PatchOutcome(proposal.id, proposal.target, True, True,
                        f"checks green, kept as {rev_id}",
                        cand.base_rev, manifest, assessment_id)


def sync_worktree_to_accepted(repo: Path, store: "promotemod.Store",
                              journal: Journal | None = None,
                              ignore: tuple[str, ...] = ()) -> None:
    """Bring the live tree to the latest accepted snapshot. Halts on drift."""
    repo = Path(repo)
    rev_id = store.latest_rev()
    manifest = store.rev_manifest(rev_id) if rev_id else None
    if rev_id is None or manifest is None:
        raise RuntimeError("promotion store has no accepted revision to sync")
    live = promotemod.hash_tree(repo, ignore)
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
    ok, divergent = promotemod.worktree_matches(store, repo, ignore)
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
    ignore = _record_ignore(repo, assess_dir)
    if store.latest_rev() is None:
        if not tree_clean(repo):
            emit(journal, "revise", "aborted", "tree not clean")
            report.skipped = len(doc.proposals)
            return report
        store.init_from_worktree(repo, ignore)
    else:
        ok, divergent = promotemod.worktree_matches(store, repo, ignore)
        if not ok:
            emit(journal, "revise", "aborted",
                 f"worktree differs from {store.latest_rev()}: {divergent}")
            report.skipped = len(doc.proposals)
            return report
    checks_log = (Path(assess_dir) / f"checks-{assessment_id}.log"
                  if assess_dir is not None else None)
    if not checks_pass(repo, check_cmd, journal, log_path=checks_log):
        emit(journal, "revise", "aborted", "baseline checks not green")
        report.skipped = len(doc.proposals)
        return report
    for proposal in doc.proposals[:MAX_PROPOSALS_PER_ROUND]:
        context_record: dict = {}
        attempts: list = []
        try:
            diff = request_applicable_diff(proposal, client, repo, context_record,
                                           journal, attempts_out=attempts)
        except RequestBlocked:
            raise
        except ChangesetError as e:
            reason = f"diff request failed: {e}"
            if assess_dir is not None:
                save_candidate(assess_dir, proposal, "", None, context_record,
                               assessment_id, "diff-failed", reason, attempts)
            report.outcomes.append(PatchOutcome(proposal.id, proposal.target, False, False, reason))
            report.skipped += 1
            continue
        except Exception as e:
            report.outcomes.append(PatchOutcome(proposal.id, proposal.target, False, False, f"diff request failed: {e}"))
            report.skipped += 1
            continue
        oc = apply_one(repo, proposal, diff, check_cmd, journal,
                       assessment_id, assess_dir, context_record, attempts)
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
    require_main_branch(repo)  # auto-branches cut from main only
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


class BridgeRefused(RuntimeError):
    """Container publish bridge refused: tamper, policy, drift, or dirty tree."""


BRIDGE_MAX_DIFF_BYTES = 100_000


@dataclass
class BridgeCandidate:
    proposal_id: str
    target: str
    change: str
    reason: str
    assessment_id: str
    detail: str
    diff: str  # normalized, verified text to apply
    manifest: dict
    drift: str = ""


def load_bridge_candidates(bundle: str | Path) -> list[dict]:
    """Kept-candidate records from a container export bundle."""
    cdir = Path(bundle) / "assessments" / "candidates"
    if not cdir.is_dir():
        raise BridgeRefused(f"no candidates dir in {bundle}")
    out = []
    for path in sorted(cdir.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            raise BridgeRefused(f"{path.name}: unreadable candidate: {e}") from e
        if not isinstance(entry, dict):
            raise BridgeRefused(f"{path.name}: candidate is not an object")
        entry["_source"] = path.name
        out.append(entry)
    return out


def verify_bridge_candidate(entry: dict) -> BridgeCandidate:
    """A kept candidate whose diff is intact, bounded, and policy-clean."""
    src = entry.get("_source", "?")
    if entry.get("status") != "kept":
        raise BridgeRefused(f"{src}: not kept (status={entry.get('status')!r})")
    raw = entry.get("raw_diff")
    if not isinstance(raw, str) or not raw.strip():
        raise BridgeRefused(f"{src}: empty diff")
    if len(raw.encode()) > BRIDGE_MAX_DIFF_BYTES:
        raise BridgeRefused(f"{src}: diff exceeds {BRIDGE_MAX_DIFF_BYTES} bytes")
    manifest = entry.get("manifest")
    if not isinstance(manifest, dict):
        raise BridgeRefused(f"{src}: missing manifest")
    want = manifest.get("diff_sha")
    got = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    if want != got:
        raise BridgeRefused(f"{src}: diff_sha mismatch (tampered export?)")
    try:
        norm = normalize_unified_diff(raw)
        ops = parse_unified_diff(norm)
        check_policy(ops, target_allowed)
    except ChangesetError as e:
        raise BridgeRefused(f"{src}: {e}") from e
    paths = {op.new_path or op.old_path for op in ops}
    target = entry.get("target", "")
    if target not in paths:
        raise BridgeRefused(f"{src}: declared target {target!r} not in diff")
    return BridgeCandidate(
        proposal_id=str(entry.get("proposal_id", src)),
        target=str(target),
        change=str(entry.get("change", ""))[:1000],
        reason=str(entry.get("reason", ""))[:500],
        assessment_id=str(entry.get("assessment_id", "")),
        detail=str(entry.get("detail", ""))[:300],
        diff=norm, manifest=manifest)


def _file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def apply_bridge_candidates(repo: str | Path, candidates: list[BridgeCandidate]) -> None:
    """Apply verified diffs to the worktree; roll back and refuse on failure."""
    repo = Path(repo)
    applied: list[tuple[str, bool]] = []  # (path, existed_before)
    try:
        touched: list[str] = []
        for cand in candidates:
            touched.extend(op.new_path or op.old_path
                           for op in parse_unified_diff(cand.diff))
        # Once, up front: later candidates legitimately see earlier applies.
        dirty = worktree_status_paths(repo, *touched) if touched else set()
        if dirty:
            raise BridgeRefused(f"worktree not clean: {sorted(dirty)}")
        for cand in candidates:
            ops = parse_unified_diff(cand.diff)
            paths = [op.new_path or op.old_path for op in ops]
            pre = {p: _file_sha(repo / p) for p in paths}
            for check in (True, False):
                args = ["git", "apply"] + (["--check"] if check else []) + ["-"]
                r = subprocess.run(args, cwd=repo, input=cand.diff, capture_output=True,
                                   text=True, timeout=60)
                if r.returncode != 0:
                    raise BridgeRefused(
                        f"{cand.proposal_id}: git apply failed: {r.stderr.strip()[:300]}")
            applied.extend((p, pre[p] is not None) for p in paths)
            new_files = {f["path"]: f for f in cand.manifest.get("files", [])
                         if isinstance(f, dict)}
            notes = []
            for p in paths:
                post = _file_sha(repo / p)
                want = new_files.get(p, {}).get("new_sha")
                if post is None:
                    notes.append(f"{p}: missing after apply")
                elif isinstance(want, str) and want not in ("", "absent") and post != want:
                    if pre[p] != new_files[p].get("old_sha"):
                        notes.append(f"{p}: base drift (guest-tested bytes differ)")
                    else:
                        notes.append(f"{p}: result differs from guest-tested bytes")
            cand.drift = "; ".join(notes)
    except BridgeRefused:
        for path, existed in applied:
            try:
                if existed:
                    subprocess.run(["git", "checkout", "--", path], cwd=repo,
                                   capture_output=True, timeout=60)
                else:
                    (repo / path).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _bundle_run_id(bundle: Path) -> str:
    try:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        if isinstance(manifest.get("run_id"), str) and manifest["run_id"]:
            return manifest["run_id"]
    except (ValueError, OSError):
        pass
    return bundle.name


def publish_container_bundle(repo: str | Path, bundle: str | Path, run_id: str,
                             journal: Journal, gh,
                             merge_timeout_s: float = 900,
                             merge_interval_s: float = 20) -> PublishReport:
    """Bridge: kept guest diffs from an export bundle to an auto-PR on main."""
    require_revision_trust()
    repo, bundle = Path(repo), Path(bundle)
    require_main_branch(repo)
    scan = subprocess.run([sys.executable, str(repo / "scripts" / "scan_export.py"),
                           str(bundle)], capture_output=True, text=True, timeout=300)
    if scan.returncode != 0:
        raise BridgeRefused(
            f"export scan failed: {(scan.stdout + scan.stderr).strip()[-300:]}")
    entries = load_bridge_candidates(bundle)
    kept = [verify_bridge_candidate(e) for e in entries if e.get("status") == "kept"]
    rest = [e for e in entries if e.get("status") != "kept"]
    apply_bridge_candidates(repo, kept)
    guest_run = _bundle_run_id(bundle)
    lines = [f"Container maintenance {guest_run} (bundle `{bundle.name}`).", "",
             f"Kept: {len(kept)}, not kept: {len(rest)}. "
             "Diffs verified against candidate manifests; export scan clean."]
    for c in kept:
        lines.append(f"- {c.proposal_id} `{c.target}`: {c.reason}"
                     + (f" [{c.drift}]" if c.drift else ""))
    doc = AssessmentDoc("\n".join(lines),
                        tuple(Proposal(c.proposal_id, c.target, c.change, c.reason)
                              for c in kept))
    outcomes = [PatchOutcome(c.proposal_id, c.target, True, True, c.detail or c.reason,
                             base_rev=str(c.manifest.get("base_rev", "")),
                             manifest=c.manifest, assessment_id=c.assessment_id)
                for c in kept]
    reverted = skipped = 0
    for e in rest:
        if e.get("status") == "rejected":
            reverted += 1
        else:
            skipped += 1
        outcomes.append(PatchOutcome(str(e.get("proposal_id", e.get("_source", "?"))),
                                     str(e.get("target", "?")), False, False,
                                     str(e.get("detail", "") or e.get("status", ""))[:300]))
    rep = ReviseReport(kept=len(kept), reverted=reverted, skipped=skipped,
                       outcomes=outcomes)
    return publish_round(repo, run_id, doc, rep, journal, gh,
                         merge_timeout_s, merge_interval_s)
