"""Changeset: parse and validate candidate diffs before anything executes.

Two independent validators must agree: this module's structural parser (every
file operation explicit and policy-checked) and git itself in a disposable
worktree (the diff applies to the named base). Either rejection stops the
candidate without touching the worktree.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_DIFF_FILES = 5
MAX_DIFF_LINES = 300

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_GIT_DIFF_RE = re.compile(r"^diff --git (\S+) (\S+)$")


class ChangesetError(Exception):
    """The candidate diff is malformed, out of policy, or stale. No retry."""


@dataclass(frozen=True)
class FileOp:
    old_path: str  # "" when added
    new_path: str  # "" when deleted
    op: str  # added | deleted | modified
    new_mode: str = ""


@dataclass(frozen=True)
class Manifest:
    base_rev: str
    diff_sha: str
    files: tuple[dict, ...]  # [{path, op, old_sha, new_sha}]

    def to_dict(self) -> dict:
        return {"base_rev": self.base_rev, "diff_sha": self.diff_sha,
                "files": [dict(f) for f in self.files]}


def _unquote(token: str) -> str:
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        raise ChangesetError(f"quoted path rejected: {token[:80]}")
    return token


def parse_unified_diff(diff: str) -> list[FileOp]:
    """Structural parse of every file section. Anything exotic is rejected."""
    if not diff.strip():
        raise ChangesetError("empty diff")
    if len(diff.splitlines()) > MAX_DIFF_LINES:
        raise ChangesetError(f"diff too large (cap {MAX_DIFF_LINES} lines)")
    ops: list[FileOp] = []
    if re.search(r"(?m)^diff --git ", diff):
        sections = re.split(r"(?m)^(?=diff --git )", diff)[1:]
    else:
        sections = [diff]  # headerless single-file diff: ---/+++ carry the paths
    for section in sections:
        header = section.splitlines()[0] if section.splitlines() else ""
        match = _GIT_DIFF_RE.match(header)
        if match:
            old_token, new_token = _unquote(match.group(1)), _unquote(match.group(2))
        elif re.search(r"(?m)^diff --git ", diff):
            raise ChangesetError(f"malformed diff header: {header[:80]}")
        else:
            old_token, new_token = "", ""  # resolved from ---/+++ below, else rejected
        for marker in ("rename from", "rename to", "copy from", "copy to"):
            if re.search(rf"(?m)^{marker} ", section):
                raise ChangesetError(f"{marker} rejected: renames/copies are not supported")
        if "GIT binary patch" in section or re.search(r"(?m)^Binary files ", section):
            raise ChangesetError("binary content rejected")
        if re.search(r"(?m)^old mode|^new mode", section):
            raise ChangesetError("mode changes rejected")
        new_file = re.search(r"(?m)^new file mode (\S+)", section)
        if new_file and new_file.group(1) != "100644":
            raise ChangesetError(f"new file mode {new_file.group(1)} rejected "
                                 "(no executables, symlinks, or submodules)")
        if re.search(r"(?m)^deleted file mode", section):
            raise ChangesetError("file deletion rejected: remove files explicitly, not by patch")
        old_side = re.search(r"(?m)^--- (\S+)", section)
        new_side = re.search(r"(?m)^\+\+\+ (\S+)", section)
        old_path = _strip_prefix(_unquote(old_side.group(1)) if old_side else old_token, "a/")
        new_path = _strip_prefix(_unquote(new_side.group(1)) if new_side else new_token, "b/")
        if old_path != new_path and old_path and new_path:
            raise ChangesetError(f"path alias rejected: {old_path} vs {new_path}")
        if not old_path and not new_path:
            raise ChangesetError("diff section names no path")
        for line in section.splitlines():
            if line.startswith("@@") and not _HUNK_RE.match(line):
                raise ChangesetError(f"malformed hunk header: {line[:80]}")
        if new_path and not old_path:
            ops.append(FileOp(old_path="", new_path=new_path, op="added"))
        elif old_path and not new_path:
            ops.append(FileOp(old_path=old_path, new_path="", op="deleted"))
        else:
            ops.append(FileOp(old_path=old_path, new_path=new_path, op="modified"))
    if len(ops) > MAX_DIFF_FILES:
        raise ChangesetError(f"diff touches {len(ops)} files (cap {MAX_DIFF_FILES})")
    paths = [op.new_path or op.old_path for op in ops]
    if len(set(paths)) != len(paths):
        raise ChangesetError("diff touches the same path twice")
    return ops


def _strip_prefix(token: str, prefix: str) -> str:
    if token == "/dev/null":
        return ""
    if token.startswith(prefix):
        return token[len(prefix):]
    return token


FORBIDDEN_PREFIXES = ("tests/", ".github/", "boundary/", "scripts/", "recovery/",
                        "validation/", "evalpack/")
FORBIDDEN_NAMES = ("Dockerfile", ".dockerignore")
FORBIDDEN_FILES = ("src/canary/revise.py", "src/canary/muse_client.py",
                   "src/canary/changeset.py", "pyproject.toml",
                   "docs/TRUST_BOUNDARY.md", "docs/TOKEN_BOUNDARY.md")


def target_allowed(target: str) -> str | None:
    """None if allowed, else the reason it is forbidden.

    The durable promotion policy: tests, evaluation, launcher, broker,
    credentials, workflows, lockfiles, recovery evidence, and the gate itself
    are outside worker control. Only src/canary/ worker code may be patched.
    Shared by the revise gate and assessment-target validation.
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


def check_policy(ops: list[FileOp], target_allowed) -> None:
    """Every touched path against the external policy. First violation raises."""
    for op in ops:
        path = op.new_path or op.old_path
        reason = target_allowed(path)
        if reason:
            raise ChangesetError(f"forbidden target {path}: {reason}")
        if op.op == "deleted":
            raise ChangesetError(f"file deletion rejected: {op.old_path}")


def _git(repo: Path, *args: str, input_text: str | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                       timeout=120, input=input_text, stdin=subprocess.DEVNULL if input_text is None else None)
    if r.returncode != 0:
        raise ChangesetError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def verify_in_disposable(repo: str | Path, diff: str, ops: list[FileOp],
                         prior_diffs: tuple[str, ...] = ()) -> Manifest:
    """Apply onto a pristine base+prior directory copy; hash the result.

    Proves with git itself that the diff is well-formed and applies to the
    named base. The copy is a plain directory — never a git worktree linked
    to host git metadata. Nothing here touches the real worktree.
    """
    repo = Path(repo)
    base_rev = _git(repo, "rev-parse", "HEAD").strip()
    work = Path(tempfile.mkdtemp(prefix="canary-changeset-"))
    # Plain copy of the live base: tracked content plus untracked files
    # under src/ and tests/ (kept patches land uncommitted, and follow-up
    # diffs must verify against them). No .git, no runs, no junk.
    tracked = _git(repo, "ls-files", "-z").split("\0")
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard",
                     "-z", "--", "src", "tests").split("\0")
    try:
        for rel in tracked + untracked:
            if not rel:
                continue
            src = repo / rel
            dst = work / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        for prior in prior_diffs:
            _git(work, "apply", "--check", "-", input_text=prior)
            _git(work, "apply", "-", input_text=prior)
        _git(work, "apply", "--check", "-", input_text=diff)
        old_shas: dict[str, str] = {}
        for op in ops:
            target = work / (op.new_path or op.old_path)
            old_shas[op.new_path or op.old_path] = (
                hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else "absent")
        _git(work, "apply", "-", input_text=diff)
        files = []
        for op in ops:
            path = op.new_path or op.old_path
            target = work / path
            files.append({"path": path, "op": op.op, "old_sha": old_shas[path],
                          "new_sha": hashlib.sha256(target.read_bytes()).hexdigest()
                          if target.exists() else "absent"})
        return Manifest(base_rev=base_rev,
                        diff_sha="sha256:" + hashlib.sha256(diff.encode()).hexdigest(),
                        files=tuple(files))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def worktree_status_paths(repo: Path, *scopes: str) -> set[str]:
    out = _git(repo, "status", "--porcelain", "--", *scopes)
    paths = set()
    for line in out.splitlines():
        parts = line[3:].strip().split(" -> ")
        paths.update(p.strip().strip('"') for p in parts if p.strip())
    return paths
