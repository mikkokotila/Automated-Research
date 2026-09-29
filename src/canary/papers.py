"""Paper: one retrieved candidate with provenance."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Paper:
    ref: str  # stable id: doi if present else source:id
    title: str
    abstract: str = ""
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str = ""
    doi: str = ""
    url: str = ""
    citations: int = 0
    source: str = ""  # "openalex" | "arxiv"
    score: float = 0.0
    evidence: str = ""  # "fulltext" | "abstract" | "none" ("" = unknown/legacy)
    oa_url: str = ""  # lawful open-access copy when the provider names one
    license: str = ""  # license string as reported by the provider, if any
    identifiers: dict = field(default_factory=dict, compare=False)
    extra: dict = field(default_factory=dict, compare=False)

    def cite_line(self, n: int) -> str:
        authors = ", ".join(self.authors[:3])
        if len(self.authors) > 3:
            authors += " et al."
        year = self.year or "n.d."
        return f"[{n}] {self.title} — {authors} ({year}). {self.url or self.doi}".strip()
