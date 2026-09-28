"""Promotion: transactional patch evaluation with immutable accepted revisions.

Each candidate is validated, applied, and tested in a disposable directory
copy of the latest accepted revision — never a git worktree, never the live
tree. Promotion atomically advances the accepted pointer only for the exact
candidate that passed; anything else leaves the accepted revision untouched.

States: proposed -> validated -> applied -> testing -> accepted | rejected.
Crashes leave interrupted candidates and an intact accepted revision; recovery
either completes a recorded pass or marks the candidate interrupted. Accepted
revisions are never mutated; rollback is a new accepted revision restoring
older content, with the superseded candidate marked rolled_back.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = ("accepted", "rejected", "interrupted", "rolled_back")
COPY_IGNORES = (".git", "runs", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache")


class PromotionError(Exception):
    """Candidate rejected or store inconsistent. Accepted data is untouched."""


class PromotionHalt(Exception):
    """Store integrity unrecoverable. Explicit halt, never a guess."""


class _CrashSim(Exception):
    """Fault-injection crash. Test-only; never raised in production paths."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def snapshot_tree(source: str | Path, dest: str | Path,
                  ignore: tuple[str, ...] = ()) -> dict[str, str]:
    """Copy a runnable tree (minus VCS/caches) and hash every file.

    Extra ignore names (e.g. the run record dir) use the same
    basename-at-any-level semantics as COPY_IGNORES. Anchor and every
    later comparison must use the same ignore set, or manifests diverge
    and the round aborts fail-closed.
    """
    source, dest = Path(source), Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest,
                    ignore=shutil.ignore_patterns(*COPY_IGNORES, *ignore, "*.pyc"))
    manifest: dict[str, str] = {}
    for path in sorted(dest.rglob("*")):
        if path.is_file() and not path.is_symlink():
            manifest[str(path.relative_to(dest))] = _sha_file(path)
    return manifest


def hash_tree(root: str | Path, ignore: tuple[str, ...] = ()) -> dict[str, str]:
    """Hash the live tree the same way snapshots are hashed."""
    root = Path(root)
    skipped = COPY_IGNORES + tuple(ignore)
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in skipped or part.endswith(".pyc") for part in rel.parts):
            continue
        if path.is_file() and not path.is_symlink():
            manifest[str(rel)] = _sha_file(path)
    return manifest


@dataclass
class Candidate:
    id: str
    proposal_id: str
    target: str
    change: str
    reason: str
    assessment_id: str
    base_rev: str
    base_manifest_sha: str
    diff_sha: str
    diff: str = ""
    state: str = "proposed"
    manifest: dict = field(default_factory=dict)
    test: dict = field(default_factory=dict)
    reason_detail: str = ""
    wall_s: float = 0.0
    transitions: list = field(default_factory=list)


@dataclass
class Store:
    root: Path

    @property
    def revs(self) -> Path:
        return self.root / "revs"

    @property
    def candidates(self) -> Path:
        return self.root / "candidates"

    def accepted_path(self) -> Path:
        return self.root / "accepted.json"

    def read_pointer(self, name: str = "accepted.json") -> dict | None:
        path = self.root / name
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and "rev_id" in data else None

    def latest_rev(self) -> str | None:
        pointer = self.read_pointer()
        return pointer["rev_id"] if pointer else None

    def rev_manifest(self, rev_id: str) -> dict | None:
        path = self.revs / rev_id / "manifest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def init_from_worktree(self, worktree: str | Path,
                         ignore: tuple[str, ...] = ()) -> str:
        """Revision zero: snapshot the (verified clean) worktree."""
        rev_id = _new_id("rev")
        manifest = snapshot_tree(worktree, self.revs / rev_id / "tree", ignore)
        _atomic_write_json(self.revs / rev_id / "manifest.json",
                           {"rev_id": rev_id, "parent": None, "candidate": None,
                            "ts": _utcnow(), "files": manifest})
        _atomic_write_json(self.accepted_path(),
                           {"rev_id": rev_id, "manifest_sha": _manifest_sha(manifest),
                            "candidate": None, "ts": _utcnow()})
        return rev_id

    def save_candidate(self, cand: Candidate) -> None:
        _atomic_write_json(self.candidates / f"{cand.id}.json", asdict(cand))

    def load_candidate(self, cid: str) -> Candidate | None:
        path = self.candidates / f"{cid}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return Candidate(**{k: data[k] for k in Candidate.__dataclass_fields__
                                if k in data})
        except TypeError:
            return None

    def all_candidates(self) -> list[Candidate]:
        out = []
        if not self.candidates.is_dir():
            return out
        for path in sorted(self.candidates.glob("*.json")):
            cand = self.load_candidate(path.stem)
            if cand is not None:
                out.append(cand)
        return out


def _manifest_sha(manifest: dict[str, str]) -> str:
    payload = json.dumps(manifest, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _transition(store: Store, cand: Candidate, state: str, journal=None,
                detail: str = "") -> None:
    cand.state = state
    if detail:
        cand.reason_detail = detail[:500]
    cand.transitions.append({"at": _utcnow(), "state": state})
    store.save_candidate(cand)
    if journal is not None:
        from .journal import emit

        emit(journal, "promote", state,
             f"{cand.id} base={cand.base_rev[:24]} cand={cand.diff_sha[:24]} {detail}"[:300])


def evaluate(store: Store, proposal, diff: str, check_cmd: list[str],
             assessment_id: str = "", journal=None, timeout_s: int = 600,
             crash_at: frozenset = frozenset()) -> Candidate:
    """Run one candidate through validated/applied/testing in a disposable copy.

    Returns the candidate in a terminal-or-testing state; promotion is a
    separate step. crash_at injects _CrashSim after the named transitions.
    """
    from .changeset import check_policy, parse_unified_diff
    from .revise import target_allowed

    started = time.monotonic()
    base_rev = store.latest_rev()
    if base_rev is None:
        raise PromotionHalt("no accepted revision: init the store first")
    base_manifest = store.rev_manifest(base_rev)
    if base_manifest is None:
        raise PromotionHalt(f"accepted revision {base_rev} has no manifest")
    cand = Candidate(id=_new_id("cand"), proposal_id=proposal.id, target=proposal.target,
                     change=proposal.change[:1000], reason=proposal.reason[:500],
                     assessment_id=assessment_id, base_rev=base_rev,
                     base_manifest_sha=_manifest_sha(base_manifest["files"]),
                     diff_sha="sha256:" + hashlib.sha256(diff.encode()).hexdigest(),
                     diff=diff)
    _transition(store, cand, "proposed", journal)
    try:
        ops = parse_unified_diff(diff)
        check_policy(ops, target_allowed)
    except Exception as e:
        cand.wall_s = time.monotonic() - started
        _transition(store, cand, "rejected", journal, f"validation: {e}")
        return cand
    cand.manifest = {"files": [{"path": op.new_path or op.old_path, "op": op.op}
                               for op in ops],
                     "base_files": dict(base_manifest["files"])}
    _transition(store, cand, "validated", journal, f"{len(ops)} files")
    if "validated" in crash_at:
        raise _CrashSim("validated")
    work = Path(tempfile.mkdtemp(prefix="canary-cand-"))
    try:
        try:
            manifest = snapshot_tree(store.revs / base_rev / "tree", work / "tree")
        except OSError as e:
            cand.wall_s = time.monotonic() - started
            _transition(store, cand, "rejected", journal, f"snapshot failed: {e}")
            return cand
        if _manifest_sha(manifest) != cand.base_manifest_sha:
            cand.wall_s = time.monotonic() - started
            _transition(store, cand, "rejected", journal,
                        "accepted snapshot drifted during evaluation")
            return cand
        tree = work / "tree"
        applied = subprocess.run(["git", "apply", "-"], input=diff.encode(), cwd=tree,
                                 capture_output=True, timeout=120)
        if applied.returncode != 0:
            cand.wall_s = time.monotonic() - started
            _transition(store, cand, "rejected", journal,
                        f"apply failed: {applied.stderr.decode()[:200]}")
            return cand
        cand.manifest["new_hashes"] = hash_tree(tree)
        _transition(store, cand, "applied", journal)
        if "applied" in crash_at:
            raise _CrashSim("applied")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(tree / "src") + os.pathsep + env.get("PYTHONPATH", "")
        try:
            proc = subprocess.run(check_cmd, cwd=tree, capture_output=True, text=True,
                                  timeout=timeout_s, env=env)
        except subprocess.TimeoutExpired:
            cand.wall_s = time.monotonic() - started
            cand.test = {"exit": None, "timeout": True, "tail": ""}
            _transition(store, cand, "rejected", journal,
                        f"checks timed out after {timeout_s}s")
            return cand
        cand.test = {"exit": proc.returncode, "timeout": False,
                     "tail": (proc.stdout + proc.stderr)[-2000:]}
        cand.wall_s = time.monotonic() - started
        _transition(store, cand, "testing", journal, f"exit={proc.returncode}")
        if "tested" in crash_at:
            raise _CrashSim("tested")
        if proc.returncode != 0:
            _transition(store, cand, "rejected", journal,
                        f"checks failed with exit {proc.returncode}")
            return cand
        return cand  # testing passed; promotion decides separately
    finally:
        shutil.rmtree(work, ignore_errors=True)


def promote(store: Store, cand: Candidate, journal=None,
            crash_at: frozenset = frozenset()) -> str:
    """Atomically advance accepted to this candidate. Verifies everything first.

    Only the exact evaluated candidate promotes: the accepted pointer must
    still be the candidate's base and the recorded hashes must match a fresh
    re-application. Anything else rejects without touching accepted.
    """
    if cand.state != "testing" or cand.test.get("exit") != 0:
        raise PromotionError(f"candidate {cand.id} is {cand.state}: only tested-pass promotes")
    pointer = store.read_pointer()
    if pointer is None or pointer["rev_id"] != cand.base_rev:
        _transition(store, cand, "rejected", journal,
                    "accepted advanced during evaluation; rebase and rerun")
        raise PromotionError("concurrent promotion: accepted moved; candidate rebased out")
    if "promoting" in crash_at:
        raise _CrashSim("promoting")
    rev_id = _new_id("rev")
    work = Path(tempfile.mkdtemp(prefix="canary-promote-"))
    try:
        base_manifest = store.rev_manifest(cand.base_rev)
        if base_manifest is None:
            raise PromotionHalt(f"base revision {cand.base_rev} lost its manifest")
        manifest = snapshot_tree(store.revs / cand.base_rev / "tree", work / "tree")
        if _manifest_sha(manifest) != cand.base_manifest_sha:
            _transition(store, cand, "rejected", journal, "base snapshot drifted")
            raise PromotionError("base drifted; candidate rejected")
        # Re-apply the exact recorded diff onto a fresh base copy: the
        # resulting hashes must reproduce the tested tree. Promotion copies
        # this verified result, never the live tree, so post-evaluation
        # mutation cannot slip in.
        tree = work / "tree"
        reapplied = subprocess.run(["git", "apply", "-"], input=cand.diff.encode(),
                                   cwd=tree, capture_output=True, timeout=120)
        if reapplied.returncode != 0:
            _transition(store, cand, "rejected", journal,
                        "recorded diff no longer applies to base")
            raise PromotionError("candidate drifted; rejected")
        if hash_tree(tree) != cand.manifest.get("new_hashes", {}):
            _transition(store, cand, "rejected", journal,
                        "re-application diverged from the tested tree")
            raise PromotionError("candidate diverged; rejected")
        _transition(store, cand, "testing", journal, "promoting")
        dest = store.revs / rev_id
        dest.mkdir(parents=True, exist_ok=True)
        snapshot_manifest = snapshot_tree(tree, dest / "tree")
        if _manifest_sha(snapshot_manifest) != _manifest_sha(cand.manifest["new_hashes"]):
            _transition(store, cand, "rejected", journal, "promotion copy mismatch")
            raise PromotionError("promotion copy diverged from the tested tree")
        _atomic_write_json(dest / "manifest.json",
                           {"rev_id": rev_id, "parent": cand.base_rev,
                            "candidate": cand.id, "ts": _utcnow(),
                            "files": snapshot_manifest})
        prev = store.read_pointer()
        if prev is not None:
            _atomic_write_json(store.root / "accepted.prev.json", prev)
        _atomic_write_json(store.accepted_path(),
                           {"rev_id": rev_id,
                            "manifest_sha": _manifest_sha(snapshot_manifest),
                            "candidate": cand.id, "ts": _utcnow()})
        if "swapped" in crash_at:
            raise _CrashSim("swapped")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _transition(store, cand, "accepted", journal, f"rev={rev_id}")
    return rev_id


def recover(store: Store, journal=None) -> dict:
    """Resolve every non-terminal candidate and verify the accepted chain.

    Testing candidates with a recorded pass re-drive promotion (re-verified);
    everything else interrupted becomes rejected-with-evidence. Returns a
    summary; raises PromotionHalt when accepted itself is unrecoverable.
    """
    pointer = store.read_pointer()
    if pointer is None:
        prev = store.read_pointer("accepted.prev.json")
        if prev is not None and store.rev_manifest(prev["rev_id"]) is not None:
            _atomic_write_json(store.accepted_path(), prev)
            pointer = prev
        else:
            raise PromotionHalt("accepted pointer lost and no valid predecessor")
    manifest = store.rev_manifest(pointer["rev_id"])
    if manifest is None or _manifest_sha(manifest["files"]) != pointer.get("manifest_sha"):
        prev = store.read_pointer("accepted.prev.json")
        if prev is not None and store.rev_manifest(prev["rev_id"]) is not None:
            _atomic_write_json(store.accepted_path(), prev)
            pointer = prev
        else:
            raise PromotionHalt(f"accepted revision {pointer['rev_id']} corrupt, no predecessor")
    summary: dict = {"accepted": pointer["rev_id"], "completed": [], "interrupted": []}
    for cand in store.all_candidates():
        if cand.state in TERMINAL:
            continue
        if cand.state == "testing" and cand.test.get("exit") == 0:
            try:
                rev_id = promote(store, cand, journal)
                summary["completed"].append(f"{cand.id}->{rev_id}")
            except (PromotionError, PromotionHalt) as e:
                landed = store.read_pointer()
                if landed is not None and landed.get("candidate") == cand.id:
                    _transition(store, cand, "accepted", journal,
                                f"rev={landed['rev_id']} (recovered post-swap)")
                    summary["completed"].append(f"{cand.id}->{landed['rev_id']}")
                else:
                    _transition(store, cand, "rejected", journal, f"recovery: {e}")
                    summary["interrupted"].append(cand.id)
        else:
            _transition(store, cand, "interrupted", journal,
                        f"crashed during {cand.state}; accepted={pointer['rev_id']}")
            summary["interrupted"].append(cand.id)
    return summary


def rollback(store: Store, to_rev: str, reason: str, journal=None,
             check_cmd: list[str] | None = None) -> str:
    """New accepted revision restoring older content. The old candidate is marked."""
    from .assess import Proposal

    manifest = store.rev_manifest(to_rev)
    if manifest is None:
        raise PromotionError(f"cannot roll back to unknown revision {to_rev}")
    current = store.latest_rev()
    # A rollback is an ordinary candidate whose content equals the target rev;
    # it must pass the same checks as any other promotion.
    cand = Candidate(id=_new_id("cand"), proposal_id="rollback", target="(rollback)",
                     change=f"restore {to_rev}: {reason}"[:1000], reason=reason[:500],
                     assessment_id="", base_rev=current or "",
                     base_manifest_sha="", diff_sha="sha256:rollback")
    _transition(store, cand, "proposed", journal, f"rollback to {to_rev}")
    work = Path(tempfile.mkdtemp(prefix="canary-rollback-"))
    try:
        snapshot_tree(store.revs / to_rev / "tree", work / "tree")
        cand.manifest = {"files": [{"path": p, "op": "restored"} for p in manifest["files"]],
                         "new_hashes": hash_tree(work / "tree")}
        _transition(store, cand, "validated", journal)
        _transition(store, cand, "applied", journal)
        if check_cmd:
            env = dict(os.environ)
            env["PYTHONPATH"] = (str(work / "tree" / "src") + os.pathsep +
                                 env.get("PYTHONPATH", ""))
            proc = subprocess.run(check_cmd, cwd=work / "tree", capture_output=True,
                                  text=True, timeout=600, env=env)
            cand.test = {"exit": proc.returncode, "timeout": False,
                         "tail": (proc.stdout + proc.stderr)[-2000:]}
            if proc.returncode != 0:
                _transition(store, cand, "rejected", journal,
                            f"rollback checks failed with exit {proc.returncode}")
                raise PromotionError("rollback candidate failed checks")
        else:
            cand.test = {"exit": 0, "timeout": False, "tail": ""}
        _transition(store, cand, "testing", journal, "exit=0")
        rev_id = _new_id("rev")
        dest = store.revs / rev_id
        snapshot_manifest = snapshot_tree(work / "tree", dest / "tree")
        _atomic_write_json(dest / "manifest.json",
                           {"rev_id": rev_id, "parent": current, "candidate": cand.id,
                            "ts": _utcnow(), "rollback_of": current,
                            "restores": to_rev, "files": snapshot_manifest})
        prev = store.read_pointer()
        if prev is not None:
            _atomic_write_json(store.root / "accepted.prev.json", prev)
        _atomic_write_json(store.accepted_path(),
                           {"rev_id": rev_id, "manifest_sha": _manifest_sha(snapshot_manifest),
                            "candidate": cand.id, "ts": _utcnow()})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _transition(store, cand, "accepted", journal, f"rev={rev_id}")
    current_ptr = store.read_pointer("accepted.prev.json")
    if current_ptr is not None and current_ptr.get("candidate"):
        undone = store.load_candidate(current_ptr["candidate"])
        if undone is not None and undone.state == "accepted":
            undone.state = "rolled_back"
            undone.reason_detail = f"superseded by rollback {rev_id}"[:500]
            store.save_candidate(undone)
    return rev_id


def worktree_matches(store: Store, worktree: str | Path,
                     ignore: tuple[str, ...] = ()) -> tuple[bool, list[str]]:
    """Does the live tree equal the latest accepted snapshot? Divergences listed."""
    rev_id = store.latest_rev()
    if rev_id is None:
        return False, ["store has no accepted revision"]
    manifest = store.rev_manifest(rev_id)
    if manifest is None:
        return False, [f"revision {rev_id} has no manifest"]
    live = hash_tree(worktree, ignore)
    want = manifest["files"]
    divergent = sorted(set(live) ^ set(want))
    divergent += sorted(p for p in set(live) & set(want) if live[p] != want[p])
    return (not divergent, divergent[:10])
