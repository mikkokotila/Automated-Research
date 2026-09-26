"""Assess: review procedural notes, document how the system must revise."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .synthesize import Completer

SYSTEM = (
    "You are the system's reviewer. Review the procedural notes and outcome. "
    "Reply with ONLY a JSON object: {\"assessment\": \"markdown\", \"proposals\": "
    "[{\"id\": \"p1\", \"target\": \"src/canary/<file>.py\", "
    "\"change\": \"concrete edit\", \"reason\": \"why\"}]}. Rules: at most 3 "
    "proposals; each must name one file under src/canary/; never propose "
    "changes to tests, workflows, Docker files, lockfiles, or revise.py itself; "
    "prefer small, verifiable fixes over redesigns. If nothing is worth changing, "
    "use []. The assessment markdown covers: what worked, what failed, and what "
    "to change next."
)


@dataclass(frozen=True)
class Proposal:
    id: str
    target: str
    change: str
    reason: str


@dataclass(frozen=True)
class AssessmentDoc:
    markdown: str
    proposals: tuple[Proposal, ...]


def parse_assessment(text: str) -> AssessmentDoc:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return AssessmentDoc(markdown=text.strip() or "no assessment produced", proposals=())
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return AssessmentDoc(markdown=text.strip(), proposals=())
    if not isinstance(raw, dict):
        return AssessmentDoc(markdown=text.strip(), proposals=())
    out: list[Proposal] = []
    for item in raw.get("proposals", []) or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target", "")).strip()
        change = str(item.get("change", "")).strip()
        if not target or not change:
            continue
        out.append(Proposal(
            id=str(item.get("id", f"p{len(out) + 1}")),
            target=target,
            change=change[:1000],
            reason=str(item.get("reason", ""))[:500],
        ))
    md = str(raw.get("assessment", "")).strip() or "no assessment produced"
    return AssessmentDoc(markdown=md, proposals=tuple(out[:3]))


def assess(journal_text: str, outcome: str, client: Completer) -> AssessmentDoc:
    user = f"Procedural notes:\n{journal_text}\n\nOutcome:\n{outcome}\n\nAssess and propose revisions."
    return parse_assessment(client.complete(SYSTEM, user))
