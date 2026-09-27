"""Analysis narration: Muse turns model results into plain-words findings."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .data import Prepared
from .modeling import Results
from .synthesize import Completer

SYSTEM = (
    "You are a careful data scientist. Summarize the modeling results in plain "
    "words for a non-technical researcher: what was tested, what predicts the "
    "target, and how much to trust it. Use ONLY the numbers provided — never "
    "invent metrics. State warnings plainly. Keep it under 300 words. This is "
    "predictive association, not causal inference: describe features as "
    "predictive, never as causes, effects, or drivers."
)

# Explicit causal language the predictive zoo cannot answer. Association verbs
# (predict, relate, raise, increase, reduce) are deliberately NOT triggers.
CAUSAL_RE = re.compile(
    r"\b(causes?|caused|causing|causality|causal(ly| inference)?|affects?|"
    r"effects? of|impact of|attributable|counterfactual|confound(ing|ed|ers?)?)\b",
    re.IGNORECASE,
)

CAUSAL_REFUSAL = (
    "causal effect estimation is not supported: this analysis fits predictive "
    "association only. Effect estimation needs an experimental or "
    "quasi-experimental design (randomization, instruments, discontinuities, "
    "or explicit identification assumptions with confounder control) — "
    "a predictive model on observational rows cannot establish causation."
)


def classify_analysis(question: str) -> str:
    """'causal' when the question demands effect estimation, else 'predictive'."""
    return "causal" if CAUSAL_RE.search(question) else "predictive"


@dataclass(frozen=True)
class Findings:
    text: str
    model: str


def build_prompt(question: str, prep: Prepared, res: Results) -> str:
    p = prep.profile
    lines = [
        f"Research question: {question}",
        f"Kind: {res.kind} — prediction is not causation; importance is not effect size",
        f"Task: {res.task}; target: {p.target_values}",
        f"Rows: {p.n_rows}; features: {p.n_features} "
        f"({len(p.numeric_features)} numeric, {len(p.categorical_features)} categorical); "
        f"missing cells imputed: {p.missing_cells}",
        f"Preprocessing: {prep.preprocessing}",
        "",
        f"Results (metric: {res.metric}; higher=better except rmse):",
    ]
    for s in res.scores:
        lines.append(f"- {s.name}: cv {s.cv_mean:.4f} ± {s.cv_std:.4f}, test {s.test:.4f}")
    lines += [
        f"Selected: {res.best} (test {res.best_test}, baseline {res.baseline_test})",
        "",
        "Top predictive features (permutation importance; association, not causation):",
    ]
    lines.extend(f"- {name}: {val}" for name, val in res.importances)
    if res.warnings:
        lines += ["", "Warnings:"] + [f"- {w}" for w in res.warnings]
    if p.notes:
        lines += ["", "Data notes:"] + [f"- {n}" for n in p.notes]
    return "\n".join(lines)


def narrate(question: str, prep: Prepared, res: Results, client: Completer) -> Findings:
    text = client.complete(SYSTEM, build_prompt(question, prep, res))
    if not text.strip():
        raise RuntimeError("Muse API returned empty findings")
    return Findings(text=text, model=client.model)
