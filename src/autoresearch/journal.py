"""Journal: append-only procedural notes taken during every workflow."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Note:
    ts: str
    phase: str
    event: str
    detail: str = ""


class Journal:
    def __init__(self) -> None:
        self.notes: list[Note] = []

    def note(self, phase: str, event: str, detail: str = "") -> None:
        self.notes.append(Note(
            ts=datetime.now(timezone.utc).isoformat(),
            phase=phase,
            event=event,
            detail=detail[:2000],
        ))

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_text("\n".join(json.dumps(asdict(n)) for n in self.notes) + "\n", encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Journal":
        j = cls()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                d = json.loads(line)
                j.notes.append(Note(ts=d["ts"], phase=d["phase"], event=d["event"], detail=d.get("detail", "")))
        return j

    def text(self, max_chars: int = 6000) -> str:
        lines = [f"[{n.ts}] {n.phase}/{n.event} {n.detail}".rstrip() for n in self.notes]
        out = "\n".join(lines)
        return out[-max_chars:] if len(out) > max_chars else out

    def __len__(self) -> int:
        return len(self.notes)


def emit(journal: Journal | None, phase: str, event: str, detail: str = "") -> None:
    if journal is not None:
        journal.note(phase, event, detail)
