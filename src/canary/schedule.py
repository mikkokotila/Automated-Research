"""Scheduler: explicit state machine for the interleaved cycle (Build 16).

States: research -> assess -> propose/evaluate revision -> continue | restart | stop.
The scheduler decides the next action at safe checkpoints (after an
iteration, before a revision round). Every decision is journaled; the worker
(model output) never feeds the decision inputs — only runner-observed counts
do — so model text cannot steer scope, budgets, or promotion.

What the scheduler cannot do by construction:
- extend budgets: it holds no budget handle, only observed call counts.
- change goal/approval scope: the Scope is frozen from the run spec and the
  checkpoint; resume legs that narrow it (post-restart research-only) do so
  explicitly in the journal, never silently.
- promote or bypass containment: it only vetoes or permits the existing
  revise gate; the trust gate still decides.

Starvation guards: interleaved assessment/revision spend is capped
(max_revision_calls) and consecutive failed revision rounds veto further
revision, so maintenance can never crowd out research within one run.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .journal import Journal, emit
from .spec import StopReason

SCHEDULER_VERSION = 1


class ScheduleError(Exception):
    """Scheduler state is unusable. Halt, never guess."""


@dataclass(frozen=True)
class Scope:
    """Operator-authorized envelope. Frozen at run start, carried by checkpoints."""

    maintenance: bool
    repo_root: str | None
    check_cmd: tuple[str, ...] | None
    revise_rounds: int
    bandit: dict | None = None

    def to_dict(self) -> dict:
        return {"maintenance": self.maintenance, "repo_root": self.repo_root,
                "check_cmd": list(self.check_cmd) if self.check_cmd else None,
                "revise_rounds": self.revise_rounds, "bandit": self.bandit}

    @classmethod
    def from_dict(cls, data: dict) -> "Scope":
        bandit = data.get("bandit")
        if bandit is not None and not isinstance(bandit, dict):
            raise ScheduleError("unusable scope block: bandit must be a dict")
        try:
            cmd = data.get("check_cmd")
            return cls(maintenance=bool(data["maintenance"]),
                       repo_root=data.get("repo_root"),
                       check_cmd=tuple(cmd) if cmd else None,
                       revise_rounds=int(data.get("revise_rounds", 0)),
                       bandit=bandit)
        except (KeyError, TypeError, ValueError) as exc:
            raise ScheduleError(f"unusable scope block: {exc}") from exc


@dataclass(frozen=True)
class SchedulerConfig:
    assess_every: int = 1  # research iterations between interleaved assessments
    max_no_progress: int = 2  # consecutive fruitless iterations before honest stop
    max_failed_revisions: int = 2  # consecutive kept-nothing rounds before veto
    max_revision_calls: int = 8  # assess+revise model calls per run; research first
    max_restarts: int = 3  # fresh-worker legs per supervised run
    allow_revision: bool = False  # operator scope: maintenance with a repo root
    restart_on_revision: bool = True  # halt for a fresh worker after an accept


@dataclass
class Scheduler:
    """Counters plus policy. Inputs are runner-observed counts, never model text."""

    config: SchedulerConfig = field(default_factory=SchedulerConfig)
    scope: Scope = field(default_factory=lambda: Scope(False, None, None, 0))
    iters_since_assess: int = 0
    no_progress: int = 0
    failed_revisions: int = 0
    revision_calls: int = 0
    restarts: int = 0
    assess_rounds: int = 0
    gaps_open: int = 0

    def note_iteration(self, supported: bool, gaps_open: int) -> None:
        """One finished research step. Supported claims reset the no-progress count."""
        self.iters_since_assess += 1
        self.no_progress = 0 if supported else self.no_progress + 1
        self.gaps_open = gaps_open

    def note_assessment(self, calls_spent: int) -> None:
        self.assess_rounds += 1
        self.revision_calls += calls_spent
        self.iters_since_assess = 0

    def note_revision(self, kept: int, attempted: bool, calls_spent: int,
                    assessed: bool = True) -> None:
        """A revise pass (assessment included). Kept-nothing after attempting fails.

        A pass that errored before assessing (assessed=False) still spent its
        calls, but neither resets the assess cadence nor counts as failed.
        """
        self.revision_calls += calls_spent
        if not assessed:
            return
        self.assess_rounds += 1
        self.iters_since_assess = 0
        if attempted:
            self.failed_revisions = 0 if kept > 0 else self.failed_revisions + 1

    def note_restart(self) -> None:
        self.restarts += 1

    def check_stop(self) -> StopReason | None:
        """Honest stop before any new dispatch. None means the run may continue."""
        if self.no_progress >= self.config.max_no_progress:
            return StopReason.NO_PROGRESS
        return None

    def revision_veto(self) -> str | None:
        """Why revision must not run now; None means the gate may be attempted."""
        if not self.config.allow_revision:
            return "revision outside the authorized scope"
        if self.revision_calls >= self.config.max_revision_calls:
            return (f"revision spend capped ({self.revision_calls}/"
                    f"{self.config.max_revision_calls} calls); research continues")
        if self.failed_revisions >= self.config.max_failed_revisions:
            return (f"{self.failed_revisions} consecutive failed revision rounds; "
                    "research continues")
        return None

    def assess_due(self) -> bool:
        return (self.revision_veto() is None
                and self.iters_since_assess >= self.config.assess_every)

    def to_dict(self) -> dict:
        d = asdict(self.config)
        d.update({"version": SCHEDULER_VERSION, "scope": self.scope.to_dict(),
                  "iters_since_assess": self.iters_since_assess,
                  "no_progress": self.no_progress,
                  "failed_revisions": self.failed_revisions,
                  "revision_calls": self.revision_calls, "restarts": self.restarts,
                  "assess_rounds": self.assess_rounds, "gaps_open": self.gaps_open})
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Scheduler":
        if not isinstance(data, dict) or data.get("version") != SCHEDULER_VERSION:
            raise ScheduleError(
                f"scheduler state version {data.get('version') if isinstance(data, dict) else '?'} "
                f"is not {SCHEDULER_VERSION}: refusing unsafe restore")
        try:
            config = SchedulerConfig(
                assess_every=int(data.get("assess_every", 1)),
                max_no_progress=int(data.get("max_no_progress", 2)),
                max_failed_revisions=int(data.get("max_failed_revisions", 2)),
                max_revision_calls=int(data.get("max_revision_calls", 8)),
                max_restarts=int(data.get("max_restarts", 3)),
                allow_revision=bool(data.get("allow_revision", False)),
                restart_on_revision=bool(data.get("restart_on_revision", True)))
            sched = cls(config=config, scope=Scope.from_dict(data["scope"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ScheduleError(f"unusable scheduler block: {exc}") from exc
        for name in ("iters_since_assess", "no_progress", "failed_revisions",
                     "revision_calls", "restarts", "assess_rounds", "gaps_open"):
            try:
                setattr(sched, name, int(data.get(name, 0)))
            except (TypeError, ValueError) as exc:
                raise ScheduleError(f"scheduler counter {name} unusable: {exc}") from exc
        return sched


@dataclass(frozen=True)
class Decision:
    action: str  # research | assess | stop
    reason: str


def decide(sched: Scheduler) -> Decision:
    """Next action at a safe checkpoint. Journaled by the caller.

    "assess" means run the interleaved assess/revise pass now (research
    resumes after unless a restart is required); "research" means research
    only; "stop" carries the StopReason value as its reason.
    """
    trip = sched.check_stop()
    if trip is not None:
        return Decision("stop", trip.value)
    if sched.assess_due():
        return Decision("assess", f"cadence: {sched.iters_since_assess} iterations since assess")
    veto = sched.revision_veto()
    if veto is not None and sched.config.allow_revision:
        return Decision("research", f"revision vetoed ({veto}); research continues")
    return Decision("research", "research first")


def spawn_worker(bundle_dir: str | Path, expect_revision: str,
                 timeout_s: int = 1800) -> int:
    """Launch a fresh worker leg as a new process. Returns its exit code.

    Fresh import state by construction: the child re-imports the (revised)
    package instead of inheriting stale modules. The child authenticates to
    the request broker with the operator's environment, same as any resume.
    """
    cmd = [sys.executable, "-m", "canary.cli", "resume", str(bundle_dir),
           "--expect-revision", expect_revision]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return 124
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr)[-500:]
        raise ScheduleError(f"worker leg for {expect_revision} exited "
                            f"{proc.returncode}: {tail}")
    return 0


def supervise(bundle_dir: str | Path, journal: Journal | None = None,
              spawn=spawn_worker, max_restarts: int = 3) -> dict:
    """Drive restart legs until the bundle seals with a terminal status.

    Each leg runs in a fresh process (see spawn_worker). The loop is bounded:
    more restarts than max_restarts is an explicit failure, never a daemon.
    With no journal given, notes append durably to the bundle journal (never
    a clobbering re-save: the child owns the file between legs).
    """
    from . import report as reportmod

    if journal is None:
        journal, _ = Journal.inspect(Path(bundle_dir) / "journal.jsonl")
        journal.path = Path(bundle_dir) / "journal.jsonl"
        manifest0 = reportmod.read_bundle(bundle_dir)["manifest"] or {}
        journal.run_id = manifest0.get("run_id")
    restarts = 0
    while True:
        manifest = reportmod.read_bundle(bundle_dir)["manifest"] or {}
        if manifest.get("status") != StopReason.RESTART_REQUIRED.value:
            return {"restarts": restarts, "status": manifest.get("status", "unknown")}
        if restarts >= max_restarts:
            raise ScheduleError(f"{restarts} restarts exhausted the supervisor cap "
                                f"({max_restarts}); refusing to loop")
        checkpoint = reportmod.read_checkpoints(bundle_dir) or {}
        pending = checkpoint.get("restart_pending")
        if not pending:
            raise ScheduleError("bundle wants a restart but no revision is pending")
        emit(journal, "supervise", "restart",
             f"leg {restarts + 1}: fresh worker for {pending}")
        spawn(str(bundle_dir), pending)
        restarts += 1
