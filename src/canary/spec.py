"""RunSpec: versioned run configuration with hard budgets (Build 06).

The spec is frozen at parse time and validated before any external call.
Spend is tracked on a RunBudget object threaded through the run -- never in
worker-editable environment variables. Token spend is exact only when the
model client reports broker receipts; unknown pricing is never presented
as a cost (no monetary claims are made at all).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

# Locked model, owned here so both the spec and the client import one value.
# The broker carries its own copy (boundary/policy.py) and rejects mismatches.
DEFAULT_MODEL = "muse-spark-1.3-contributor"

SPEC_VERSION = 1
QUESTION_LIMIT = 2000
MAX_ITERATIONS = 5


class InvalidSpec(ValueError):
    """Run configuration rejected before anything started."""


class BudgetExhausted(RuntimeError):
    """A hard run budget ran out. Partial work stands; nothing is faked."""


class Cancelled(RuntimeError):
    """The run was cancelled. No further dispatch; in-flight step finishes."""


class StopReason(str, Enum):
    CONVERGED = "converged"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CANCELLED = "cancelled"
    PROVIDER_BLOCKED = "provider_blocked"
    INVALID_INPUT = "invalid_input"
    FAILED = "failed"


@dataclass(frozen=True)
class ResearchSpec:
    question: str
    max_papers: int = 10
    year_from: int | None = None

    def __post_init__(self) -> None:
        question = self.question.strip()
        if not question:
            raise ValueError("question must be non-empty")
        if len(question) > 2000:
            raise ValueError("question must be at most 2000 characters")
        if not 1 <= self.max_papers <= 50:
            raise ValueError("max_papers must be between 1 and 50")
        if self.year_from is not None and not 1900 <= self.year_from <= 2100:
            raise ValueError("year_from must be between 1900 and 2100")
        object.__setattr__(self, "question", question)


@dataclass(frozen=True)
class RunSpec:
    """Versioned run configuration. Invalid values fail before any call."""

    question: str
    version: int = SPEC_VERSION
    csv: str | None = None
    target: str | None = None
    max_papers: int = 5
    year_from: int | None = None
    model: str = DEFAULT_MODEL
    maintenance: bool = False
    max_iterations: int = 3
    revise_rounds: int = 1
    max_model_calls: int = 25
    max_tokens: int = 20_000_000
    wall_time_s: int = 1800
    finalize_calls: int = 1
    out_dir: str = "./out"

    def __post_init__(self) -> None:
        question = self.question.strip()
        if not question:
            raise InvalidSpec("question must be non-empty")
        if len(question) > QUESTION_LIMIT:
            raise InvalidSpec(f"question must be at most {QUESTION_LIMIT} characters")
        if self.version != SPEC_VERSION:
            raise InvalidSpec(f"unsupported spec version {self.version}")
        if self.model != DEFAULT_MODEL:
            raise InvalidSpec(f"only {DEFAULT_MODEL} is permitted")
        if bool(self.csv) != bool(self.target):
            raise InvalidSpec("csv and target must be given together")
        if not 1 <= self.max_papers <= 50:
            raise InvalidSpec("max_papers must be between 1 and 50")
        if self.year_from is not None and not 1900 <= self.year_from <= 2100:
            raise InvalidSpec("year_from must be between 1900 and 2100")
        if not 1 <= self.max_iterations <= MAX_ITERATIONS:
            raise InvalidSpec(f"max_iterations must be between 1 and {MAX_ITERATIONS}")
        if not 0 <= self.revise_rounds <= 3:
            raise InvalidSpec("revise_rounds must be between 0 and 3")
        if not 1 <= self.max_model_calls <= 10_000:
            raise InvalidSpec("max_model_calls must be between 1 and 10000")
        if not 1 <= self.max_tokens <= 200_000_000:
            raise InvalidSpec("max_tokens must be between 1 and 200000000")
        if not 1 <= self.wall_time_s <= 86_400:
            raise InvalidSpec("wall_time_s must be between 1 and 86400")
        if not 0 <= self.finalize_calls <= 5:
            raise InvalidSpec("finalize_calls must be between 0 and 5")
        if not self.out_dir.strip():
            raise InvalidSpec("out_dir must be non-empty")
        object.__setattr__(self, "question", question)

    _FIELDS = ("version", "question", "csv", "target", "max_papers", "year_from",
                 "model", "maintenance", "max_iterations", "revise_rounds",
                 "max_model_calls", "max_tokens", "wall_time_s", "finalize_calls",
                 "out_dir")

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in self._FIELDS}

    @classmethod
    def from_dict(cls, data: dict) -> "RunSpec":
        if not isinstance(data, dict):
            raise InvalidSpec("spec must be an object")
        unknown = set(data) - set(cls._FIELDS)
        if unknown:
            raise InvalidSpec(f"unknown spec fields: {sorted(unknown)}")
        try:
            return cls(**data)
        except TypeError as exc:
            raise InvalidSpec(f"invalid spec: {exc}") from exc

    def research_spec(self, question: str | None = None) -> ResearchSpec:
        return ResearchSpec(question=question or self.question,
                            max_papers=self.max_papers, year_from=self.year_from)


@dataclass
class RunBudget:
    """Mutable spend tracker. One instance per run, shared by nested work."""

    max_calls: int
    max_tokens: int
    wall_time_s: int
    calls: int = 0
    tokens: int = 0
    tokens_reported: bool = False
    cancelled: bool = False
    deadline: float = field(default=0.0)
    clock = time.monotonic

    def __post_init__(self) -> None:
        if self.deadline <= 0:
            self.deadline = self.clock() + self.wall_time_s

    @classmethod
    def from_spec(cls, spec: RunSpec, clock=time.monotonic) -> "RunBudget":
        budget = cls(spec.max_model_calls, spec.max_tokens, spec.wall_time_s)
        budget.clock = clock
        budget.deadline = clock() + spec.wall_time_s
        return budget

    def check(self) -> None:
        """Refuse further dispatch. Called before every billable step."""
        if self.cancelled:
            raise Cancelled("run was cancelled")
        if self.calls >= self.max_calls:
            raise BudgetExhausted(f"model-call budget exhausted ({self.calls}/{self.max_calls})")
        if self.tokens >= self.max_tokens:
            raise BudgetExhausted(f"token budget exhausted ({self.tokens}/{self.max_tokens})")
        if self.clock() >= self.deadline:
            raise BudgetExhausted("wall-time budget exhausted")

    def reserve_call(self) -> None:
        """Spend one call slot before dispatch; attempts are never refunded."""
        self.check()
        self.calls += 1

    def note_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        if type(prompt_tokens) is not int or type(completion_tokens) is not int:
            raise ValueError("usage must be integers")
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("usage must be non-negative")
        self.tokens += prompt_tokens + completion_tokens
        self.tokens_reported = True

    def cancel(self) -> None:
        self.cancelled = True

    def calls_remaining(self) -> int:
        return max(self.max_calls - self.calls, 0)

    def usage_summary(self) -> dict:
        return {"model_calls": self.calls, "tokens": self.tokens,
                "tokens_reported": self.tokens_reported}

    def to_dict(self) -> dict:
        """Spend-carry shape for resume (Build 08 wires the restore path)."""
        remaining = max(self.deadline - self.clock(), 0.0)
        return {"max_calls": self.max_calls, "max_tokens": self.max_tokens,
                "wall_time_s": self.wall_time_s, "calls": self.calls,
                "tokens": self.tokens, "tokens_reported": self.tokens_reported,
                "elapsed_s": self.wall_time_s - remaining}

    @classmethod
    def from_dict(cls, data: dict, clock=time.monotonic) -> "RunBudget":
        budget = cls(data["max_calls"], data["max_tokens"], data["wall_time_s"],
                     calls=data.get("calls", 0), tokens=data.get("tokens", 0),
                     tokens_reported=data.get("tokens_reported", False))
        budget.clock = clock
        budget.deadline = clock() + max(data["wall_time_s"] - data.get("elapsed_s", 0), 0)
        return budget
