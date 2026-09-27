"""Lifecycle hooks shared by review/analyze/cycle/revise (Build 16).

One helper, one artefact layout, every command. Durable notes flow through
the journal on all paths; a persisted assessment follows a completed
workflow only when enabled; revision (tree mutation) happens only via
`revise` and cycle `--maintenance` — never by default, never via `--assess`.

Artefact matrix (documented modes):

  review:                journal.jsonl, review.md, provenance.json
  review --assess:       + assessments/<id>.json + .md
  analyze:               journal.jsonl, analysis.md, analysis.json
  analyze --assess:      + assessments/<id>.json + .md
  cycle:                 + checkpoints/, iterations/, ops.jsonl, run.json,
                         synthesis.md, spec.json
  cycle --assess:        + assessments/<id>.json + .md (no tree mutation)
  cycle --maintenance:   + assessments/, candidates/, memory.jsonl, and the
                         promotion store under <repo>/runs/promotions
  revise --run-dir:      assessments/, candidates/, memory.jsonl, assessment.md
  assess:                assessments/ only (reads a past journal, changes nothing)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .assess import AssessmentError, AssessmentRecord, assess_journal
from .journal import Journal, emit
from .memory import Memory
from .synthesize import Completer


@dataclass
class Lifecycle:
    """Per-run hooks. Assessment needs a model client; notes need only a journal."""

    journal: Journal | None
    out_dir: str | Path
    assess_enabled: bool = False
    budget=None

    @property
    def assess_dir(self) -> Path:
        return Path(self.out_dir) / "assessments"

    def note(self, phase: str, event: str, detail: str = "") -> None:
        emit(self.journal, phase, event, detail)

    def finish_assessment(self, outcome: str, client: Completer,
                          code_revision: str = "unknown") -> AssessmentRecord | None:
        """Persist one post-completion assessment when enabled, else do nothing.

        Assessment failure never fails completed research: a malformed
        assessment is journaled and skipped, while budget exhaustion keeps the
        partial record assess_journal already persisted. Cancellation and
        provider blocks still propagate — those are run outcomes, not noise.
        """
        if not self.assess_enabled:
            return None
        memory = Memory(self.assess_dir / "memory.jsonl")
        notes = self.journal.notes if self.journal is not None else None
        text = self.journal.text(max_chars=60000) if self.journal is not None else ""
        try:
            record = assess_journal(notes, text, outcome, client, memory=memory,
                                    budget=self.budget, out_dir=self.assess_dir,
                                    code_revision=code_revision)
        except AssessmentError as e:
            emit(self.journal, "lifecycle", "assess-failed", str(e)[:200])
            return None
        emit(self.journal, "lifecycle", "assessed",
             f"{record.id}: {len(record.proposals)} proposals, coverage={record.status}")
        return record
