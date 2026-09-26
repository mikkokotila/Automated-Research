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
    def __init__(self, path: str | Path | None = None) -> None:
        self.notes: list[Note] = []
        self.path = Path(path) if path else None

    def note(self, phase: str, event: str, detail: str = "") -> None:
        n = Note(
            ts=datetime.now(timezone.utc).isoformat(),
            phase=phase,
            event=event,
            detail=detail[:2000],
        )
        self.notes.append(n)
        if self.path is not None:  # durable: a crash keeps everything so far
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(n)) + "\n")
            except OSError:
                pass

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
        if len(out) <= max_chars:
            return out
        head, tail = max_chars // 4, max_chars - max_chars // 4 - 60
        return out[:head] + f"\n…[{len(out) - max_chars} chars elided]…\n" + out[-tail:]

    def __len__(self) -> int:
        return len(self.notes)


def emit(journal: Journal | None, phase: str, event: str, detail: str = "") -> None:
    if journal is not None:
        journal.note(phase, event, detail)
