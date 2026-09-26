"""Synthesis: Muse turns ranked papers into a cited review."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .papers import Paper

SYSTEM = (
    "You are a precise research assistant. Write a concise literature review "
    "from ONLY the papers provided. Every factual claim must carry a citation "
    "marker like [1], [2] referring to the numbered papers. If the papers do "
    "not support an answer, say so explicitly. End with 2-4 open questions."
)

_CITE_RE = re.compile(r"\[(\d+)\]")
MAX_PROMPT_CHARS = 120_000


class Completer(Protocol):
    model: str

    def complete(self, system: str, user: str, max_tokens: int = ...) -> str: ...


@dataclass(frozen=True)
class Synthesis:
    text: str
    cited: tuple[int, ...]
    model: str


def build_prompt(question: str, papers: list[Paper]) -> str:
    lines = [f"Research question: {question}", "", "Papers:"]
    for i, p in enumerate(papers, 1):
        authors = ", ".join(p.authors[:4]) or "unknown"
        lines.append(f"[{i}] {p.title} — {authors} ({p.year or 'n.d.'}). {p.venue}".strip())
        if p.abstract:
            lines.append(f"    Abstract: {p.abstract[:1200]}")
        if p.url:
            lines.append(f"    Link: {p.url}")
    lines += ["", "Write the review with [n] citations, then open questions."]
    prompt = "\n".join(lines)
    if len(prompt) > MAX_PROMPT_CHARS:  # guard: huge paper lists must not blow context
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n[truncated for length]"
    return prompt


def cited_indices(text: str, n_papers: int) -> tuple[int, ...]:
    found = sorted({int(m) for m in _CITE_RE.findall(text) if 1 <= int(m) <= n_papers})
    return tuple(found)


def synthesize(question: str, papers: list[Paper], client: Completer) -> Synthesis:
    if not papers:
        raise ValueError("no papers to synthesize")
    text = client.complete(SYSTEM, build_prompt(question, papers))
    if not text.strip():
        raise RuntimeError("Muse API returned an empty synthesis")
    return Synthesis(text=text, cited=cited_indices(text, len(papers)), model=client.model)
