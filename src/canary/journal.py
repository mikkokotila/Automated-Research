"""Journal: versioned append-only procedural notes, durable from the first event.

Every acknowledged note is redacted, appended, and fsync'd before note()
returns. Oversized details spill to bounded sidecar files instead of being
silently cut. Worker observations and trusted supervisor facts share the
append-only log but are distinguished by a structured source field that
worker text can never forge.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .redact import redact_text

JOURNAL_VERSION = 1
DETAIL_LIMIT = 2000
MAX_SIDECARS = 100
MAX_SIDECAR_BYTES = 1_000_000


class JournalError(RuntimeError):
    """The journal is not durable (disk failure, unwritable path). Fail loud."""


@dataclass(frozen=True)
class Note:
    ts: str
    phase: str
    event: str
    detail: str = ""
    seq: int = 0
    source: str = "worker"  # "worker" | "trusted" | "legacy"
    run_id: str | None = None
    v: int = JOURNAL_VERSION


class Journal:
    def __init__(self, path: str | Path | None = None, run_id: str | None = None) -> None:
        self.notes: list[Note] = []
        self.path = Path(path) if path else None
        self.run_id = run_id
        self._seq = 0
        self._sidecars = 0

    @property
    def _sidecar_dir(self) -> Path | None:
        if self.path is None:
            return None
        return self.path.parent / (self.path.stem + "-sidecars")

    def note(self, phase: str, event: str, detail: str = "") -> None:
        self._append(phase, event, detail, "worker")

    def trusted_note(self, phase: str, event: str, detail: str = "") -> None:
        """Supervisor-side facts. Worker code paths must never call this."""
        self._append(phase, event, detail, "trusted")

    def _append(self, phase: str, event: str, detail: str, source: str) -> None:
        detail = redact_text(detail)
        self._seq += 1
        if len(detail) > DETAIL_LIMIT:
            detail = self._spill_sidecar(phase, event, detail)
        n = Note(ts=datetime.now(timezone.utc).isoformat(), phase=phase, event=event,
                 detail=detail, seq=self._seq, source=source, run_id=self.run_id)
        self.notes.append(n)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(n)) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as exc:
                raise JournalError(f"journal append not durable: {exc}") from exc

    def _spill_sidecar(self, phase: str, event: str, detail: str) -> str:
        if self._sidecar_dir is None or self._sidecars >= MAX_SIDECARS:
            kept = DETAIL_LIMIT // 2
            return detail[:kept] + f" …[{len(detail) - kept} chars dropped: sidecar unavailable]…"
        name = f"{self._seq:06d}-{phase}-{event}.txt"
        try:
            self._sidecar_dir.mkdir(parents=True, exist_ok=True)
            body = detail[:MAX_SIDECAR_BYTES]
            (self._sidecar_dir / name).write_text(body, encoding="utf-8")
        except OSError as exc:
            raise JournalError(f"sidecar not durable: {exc}") from exc
        self._sidecars += 1
        return detail[:DETAIL_LIMIT] + f" …[full text in sidecar {name}]…"

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(asdict(n)) for n in self.notes) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Journal":
        """Lenient load: keeps every decodable note, skips corrupt lines."""
        journal, _ = cls.inspect(path)
        return journal

    @classmethod
    def inspect(cls, path: str | Path) -> tuple["Journal", dict]:
        """Load with a status report; never fabricates missing history."""
        j = cls()
        corrupt: list[int] = []
        legacy = False
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return j, {"status": "missing", "notes": 0, "corrupt_lines": [],
                       "legacy": False, "truncated": False}
        except OSError as exc:
            raise JournalError(f"journal unreadable: {exc}") from exc
        for number, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("v") != JOURNAL_VERSION:
                    legacy = True
                j.notes.append(Note(ts=d["ts"], phase=d["phase"], event=d["event"],
                                    detail=d.get("detail", ""), seq=d.get("seq", 0),
                                    source=d.get("source", "legacy"),
                                    run_id=d.get("run_id"), v=d.get("v", 0)))
            except (ValueError, KeyError, TypeError):
                corrupt.append(number)
        j._seq = max([n.seq for n in j.notes] + [0])
        truncated = bool(corrupt) and corrupt[-1] == len([ln for ln in lines if ln.strip()])
        return j, {"status": "ok", "notes": len(j.notes), "corrupt_lines": corrupt,
                   "legacy": legacy, "truncated": truncated}

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
