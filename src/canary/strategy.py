"""Retrieval-strategy selection as a bounded learning experiment (#19).

One local decision: which retrieval strategy fetches a batch. A small
CPU-only epsilon-greedy bandit with per-arm ridge regression learns from
independently scored rewards; fixed and frozen modes bracket it. Default is
OFF (existing behavior, byte-identical research path); fixed adds
instrumentation only. The outcome horizon is one retrieval batch plus its
evidence assessment — no claim about long-horizon credit.

Trust split (structural, not advisory):
- The selector sees (question, pre-action features, eligible strategies)
  and returns (strategy, query). It holds no spec, budget, promotion, or
  evaluator handles and cannot alter goal, budget, runtime policy, or code.
- Rewards are computed controller-side from scored artefacts by the
  deterministic evaluator det-v1. Workers cannot edit rewards, evaluator
  rules, safety/budget controls, or the policy store; memory lessons stay
  untrusted hypotheses and never enter scoring.
- Policy snapshots and observations live in an operator-owned directory.
  Observations apply exactly once by ID (crash-safe ordering: append, then
  atomic snapshot, then in-memory commit). Corrupt, incompatible, or
  unwritable state falls back to the fixed policy, loudly journaled.

Rollback: drop the flag (off), delete the policy dir (fresh prior), or
restore snapshot.bak.json over snapshot.json. Strategy version changes
reset that arm to the documented prior instead of inheriting old wins.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

STRATEGY_VERSION = 1
FEATURE_VERSION = 1
FEATURE_DIM = 6
REWARD_VERSION = 1
SNAPSHOT_VERSION = 1
RECORD_VERSION = 1
RNG_VERSION = 1
POLICY_IMPL = "eps-greedy-ridge-v1"
EVALUATOR_ID = "det-v1"
TERM_TABLE_VERSION = 1
EPS_MAX = 0.5
RIDGE_LAMBDA = 1.0

# Predeclared reward spec. Gain rewards independently assessed evidence
# coverage for the ORIGINAL question; cost and redundancy are measured
# subtractions. Finding direction is not an input: a supported negative
# result scores exactly like a supported positive one with equal coverage.
REWARD_SPEC = {
    "version": REWARD_VERSION,
    "w_gain": 1.0,
    "w_cost": 0.5,
    "w_redundancy": 0.3,
    "gain_coverage": 0.6,
    "gain_grounding": 0.4,
    "cost_scale": 10.0,
    "cost_model_weight": 2.0,
    "red_dup": 0.5,
    "red_uncovered": 0.5,
    "clip": [-1.0, 1.0],
}

MODES = ("off", "fixed", "learning", "frozen")


class BanditError(Exception):
    """Policy store I/O failed. The run falls back to fixed, loudly."""


class PolicyCorrupt(BanditError):
    """Snapshot unreadable or incompatible. Never guessed at."""


# --- strategies: frozen set, versioned, bounded, drift-free ---

STRATEGY_IDS = ("original-v1", "focused-v1", "terms-v1", "gap-v1")

STRATEGY_DESCRIPTIONS = {
    "original-v1": "ask the original question verbatim (current fixed policy)",
    "focused-v1": "distinctive question keywords only (existing bounded refinement)",
    "terms-v1": "question plus frozen-table synonyms (bounded expansion, else fallback)",
    "gap-v1": "open evidence-gap keywords (else focused fallback)",
}

# Frozen synonym table. Reviewable, versioned, no model involved; unknown
# words fall back to the original query instead of inventing vocabulary.
EXPANSION_TABLE = {
    "mortality": ["death", "fatality", "survival"],
    "efficacy": ["effectiveness", "benefit", "outcome"],
    "prevalence": ["frequency", "occurrence", "epidemiology"],
    "risk": ["hazard", "odds", "likelihood"],
    "treatment": ["therapy", "intervention"],
    "diagnosis": ["detection", "screening", "biomarker"],
    "children": ["pediatric", "infant", "adolescent"],
    "cancer": ["tumor", "malignancy", "oncology"],
    "diabetes": ["glycemic", "insulin", "metabolic"],
    "heart": ["cardiac", "cardiovascular", "coronary"],
    "brain": ["neural", "cognitive", "cerebral"],
    "drug": ["medication", "pharmaceutical", "compound"],
}
MAX_EXPANSION_TERMS = 4
MAX_QUERY_CHARS = 500


def build_query(strategy_id: str, question: str, gaps: str = "") -> tuple[str, str | None]:
    """Build the retrieval query. Never empty; fallbacks are reported, not silent."""
    from .rank import keywords
    from .retrieval import refined_query, search_text

    if strategy_id == "original-v1":
        return question, None
    if strategy_id == "focused-v1":
        query = refined_query(question)
        if not query.strip():
            return question, "focused fell back: empty refinement"
        return query[:MAX_QUERY_CHARS], None
    if strategy_id == "terms-v1":
        hits: list[str] = []
        for word in keywords(question):
            for syn in EXPANSION_TABLE.get(word.lower(), []):
                if syn not in hits:
                    hits.append(syn)
                if len(hits) >= MAX_EXPANSION_TERMS:
                    break
            if len(hits) >= MAX_EXPANSION_TERMS:
                break
        if not hits:
            return question, "terms fell back: no table hits"
        return f"{question} {' '.join(hits)}"[:MAX_QUERY_CHARS], None
    if strategy_id == "gap-v1":
        gap_keys = keywords(gaps) if gaps.strip() else []
        if not gap_keys:
            query = refined_query(question)
            return (query[:MAX_QUERY_CHARS] if query.strip() else question,
                    "gap fell back to focused: no open gaps")
        return " ".join(gap_keys[:8])[:MAX_QUERY_CHARS], None
    raise ValueError(f"unknown strategy {strategy_id!r}")


# --- features: pre-action only, never from the action's outcome ---

def extract_features(question: str, gaps_open: bool = False, prior_coverage: float = 0.0,
                     prior_dup_rate: float = 0.0, budget_frac: float = 1.0,
                     ) -> tuple[list[float], bool]:
    """Six pre-action features. Returns (vector, clipped).

    Non-finite or out-of-range inputs are clipped into range and reported;
    the selector never crashes on unseen contexts.
    """
    from .rank import keywords

    raw = [min(1.0, len(question) / 500.0),
           min(1.0, len(keywords(question)) / 10.0),
           1.0 if gaps_open else 0.0,
           prior_coverage, prior_dup_rate, budget_frac]
    clipped = False
    out = []
    for value in raw:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            out.append(0.0)
            clipped = True
        elif value < 0.0:
            out.append(0.0)
            clipped = True
        elif value > 1.0:
            out.append(1.0)
            clipped = True
        else:
            out.append(float(value))
    return out, clipped


# --- config ---

@dataclass(frozen=True)
class BanditConfig:
    mode: str = "off"
    fixed_strategy: str = "original-v1"
    epsilon: float = 0.1
    seed: int = 0
    policy_dir: str | None = None
    eligible: tuple[str, ...] = STRATEGY_IDS

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"bandit mode must be one of {MODES}")
        if self.fixed_strategy not in STRATEGY_IDS:
            raise ValueError(f"unknown fixed strategy {self.fixed_strategy!r}")
        if not isinstance(self.epsilon, (int, float)) or not 0.0 <= self.epsilon <= EPS_MAX:
            raise ValueError(f"epsilon must be within [0, {EPS_MAX}]")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise ValueError("seed must be a non-negative int")
        if self.mode in ("learning", "frozen") and not self.policy_dir:
            raise ValueError(f"bandit mode {self.mode!r} needs a policy_dir")
        if not self.eligible or any(s not in STRATEGY_IDS for s in self.eligible):
            raise ValueError(f"eligible must be a non-empty subset of {STRATEGY_IDS}")
        if self.fixed_strategy not in self.eligible:
            raise ValueError("fixed strategy must be eligible")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["eligible"] = list(self.eligible)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "BanditConfig":
        data = dict(data)
        if isinstance(data.get("eligible"), list):
            data["eligible"] = tuple(data["eligible"])
        return cls(**data)


# --- policy store: versioned snapshots, exactly-once observations ---

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise BanditError(f"policy snapshot not durable: {exc}") from exc


def _fresh_arm() -> dict:
    eye = np.eye(FEATURE_DIM) * RIDGE_LAMBDA
    return {"A": eye.tolist(), "b": [0.0] * FEATURE_DIM,
            "strategy_version": STRATEGY_VERSION, "n": 0}


def _fresh_snapshot() -> dict:
    return {"snapshot_version": SNAPSHOT_VERSION, "policy_impl": POLICY_IMPL,
            "arms": {sid: _fresh_arm() for sid in STRATEGY_IDS},
            "applied_ids": [], "updates": 0, "decision_counter": 0,
            "rng": None, "created_at": _utcnow()}


class PolicyStore:
    """Controller-owned policy state. Workers hold no handle to this."""

    def __init__(self, path: str | Path, create: bool = True) -> None:
        self.root = Path(path)
        self.snapshot_path = self.root / "snapshot.json"
        self.backup_path = self.root / "snapshot.bak.json"
        self.obs_path = self.root / "observations.jsonl"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BanditError(f"policy dir unusable: {exc}") from exc
        if self.snapshot_path.exists():
            self.snapshot = self._load()
        elif not create:
            raise PolicyCorrupt("no snapshot for frozen evaluation; train first")
        else:
            self.snapshot = _fresh_snapshot()
            self._save()

    def _load(self) -> dict:
        try:
            snap = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PolicyCorrupt(f"snapshot unreadable: {exc}") from exc
        if not isinstance(snap, dict) or snap.get("snapshot_version") != SNAPSHOT_VERSION:
            raise PolicyCorrupt(f"snapshot version {snap.get('snapshot_version') if isinstance(snap, dict) else '?'} "
                                f"is not {SNAPSHOT_VERSION}")
        if snap.get("policy_impl") != POLICY_IMPL:
            raise PolicyCorrupt(f"policy impl {snap.get('policy_impl')!r} is not {POLICY_IMPL!r}")
        try:
            arms = snap["arms"]
            for sid in STRATEGY_IDS:
                arm = arms[sid]
                A = np.asarray(arm["A"], dtype=float)
                b = np.asarray(arm["b"], dtype=float)
                if A.shape != (FEATURE_DIM, FEATURE_DIM) or b.shape != (FEATURE_DIM,):
                    raise PolicyCorrupt(f"arm {sid} has wrong dimensions")
                if not np.all(np.isfinite(A)) or not np.all(np.isfinite(b)):
                    raise PolicyCorrupt(f"arm {sid} is non-finite")
            applied = snap["applied_ids"]
            if not isinstance(applied, list) or not all(isinstance(i, str) for i in applied):
                raise PolicyCorrupt("applied_ids malformed")
            int(snap["updates"])
            int(snap.get("decision_counter", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyCorrupt(f"snapshot malformed: {exc}") from exc
        return snap

    def _save(self) -> None:
        if self.snapshot_path.exists():
            try:
                content = self.snapshot_path.read_bytes()
                self.backup_path.write_bytes(content)
            except OSError as exc:
                raise BanditError(f"snapshot backup failed: {exc}") from exc
        _atomic_write_json(self.snapshot_path, self.snapshot)

    def weights(self, strategy_id: str) -> np.ndarray:
        arm = self.snapshot["arms"][strategy_id]
        A = np.asarray(arm["A"], dtype=float)
        b = np.asarray(arm["b"], dtype=float)
        try:
            return np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            return np.zeros(FEATURE_DIM)  # unreachable for PD A; fail safe

    def uncertainty(self, strategy_id: str, x: list[float]) -> float | None:
        """x' A^-1 x: logged for cold-start analysis, never acted on."""
        arm = self.snapshot["arms"][strategy_id]
        try:
            A = np.asarray(arm["A"], dtype=float)
            v = np.asarray(x, dtype=float)
            return float(v @ np.linalg.solve(A, v))
        except (np.linalg.LinAlgError, ValueError):
            return None

    def observe(self, obs: dict) -> dict:
        """Record an observation; apply it exactly once when eligible.

        Crash-safe ordering: validate, append the record durably, write the
        snapshot atomically, and only then commit in memory. Weights and
        applied IDs are exact across replays; a crash between the log
        append and the snapshot write can leave a visibly duplicated
        evidence line (same id twice), which replays skip by id.
        """
        obs_id = obs.get("id")
        if not obs_id or not isinstance(obs_id, str):
            return {"outcome": "rejected", "note": "observation needs a string id"}
        if obs_id in self.snapshot["applied_ids"]:
            return {"outcome": "duplicate", "note": f"{obs_id} already applied"}
        arm_id = obs.get("strategy_id")
        if arm_id not in STRATEGY_IDS:
            return {"outcome": "rejected", "note": f"unknown arm {arm_id!r}"}
        if obs.get("status") != "ok":
            self._append_obs(obs)
            return {"outcome": "recorded", "note": f"status {obs.get('status')}: kept as evidence, not applied"}
        reward = obs.get("reward")
        features = obs.get("features")
        if (not isinstance(reward, (int, float)) or isinstance(reward, bool)
                or not math.isfinite(reward)):
            return {"outcome": "rejected", "note": "reward must be a finite number"}
        if (not isinstance(features, list) or len(features) != FEATURE_DIM
                or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                       or not math.isfinite(v) for v in features)):
            return {"outcome": "rejected", "note": f"features must be {FEATURE_DIM} finite numbers"}
        arm = self.snapshot["arms"][arm_id]
        note = ""
        if arm.get("strategy_version") != STRATEGY_VERSION:
            arm = _fresh_arm()  # version changed: documented prior, no inherited wins
            note = f"arm {arm_id} reset to prior (strategy version changed); "
        x = np.asarray(features, dtype=float)
        A = np.asarray(arm["A"], dtype=float) + np.outer(x, x)
        b = np.asarray(arm["b"], dtype=float) + float(reward) * x
        self._append_obs(obs)
        self.snapshot["arms"][arm_id] = {"A": A.tolist(), "b": b.tolist(),
                                         "strategy_version": STRATEGY_VERSION,
                                         "n": int(arm.get("n", 0)) + 1}
        self.snapshot["applied_ids"] = [*self.snapshot["applied_ids"], obs_id]
        self.snapshot["updates"] = int(self.snapshot["updates"]) + 1
        self._save()
        return {"outcome": "applied", "note": note + f"{obs_id} applied as update {self.snapshot['updates']}"}

    def _append_obs(self, obs: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.obs_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obs, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            raise BanditError(f"observation not durable: {exc}") from exc

    def save_rng(self, rng: random.Random) -> None:
        version, inner, gauss = rng.getstate()
        self.snapshot["rng"] = {"rng_version": RNG_VERSION, "version": version,
                                "inner": list(inner), "gauss": gauss}
        self._save()

    def load_rng(self, seed: int) -> random.Random:
        rng = random.Random(seed)
        saved = self.snapshot.get("rng")
        if isinstance(saved, dict) and saved.get("rng_version") == RNG_VERSION:
            try:
                rng.setstate((saved["version"], tuple(saved["inner"]), saved["gauss"]))
                return rng
            except (KeyError, TypeError, ValueError):
                pass  # corrupt RNG state restarts from seed, loudly below
        return rng

    def next_decision_id(self, run_tag: str) -> str:
        counter = int(self.snapshot.get("decision_counter", 0))
        self.snapshot["decision_counter"] = counter + 1
        self._save()
        return f"{run_tag}:d{counter:06d}"


# --- reward: predeclared, deterministic, sentiment-independent ---

@dataclass(frozen=True)
class RewardResult:
    status: str  # ok | unavailable | invalid | evaluator-failed
    reward: float
    components: dict = field(default_factory=dict)
    notes: tuple[str, ...] = ()


def score_reward(question: str, papers: list, claims, validation: dict | None,
                 dedupe_stats: dict | None, n_provider_calls: int, n_model_calls: int,
                 providers_ok: bool) -> RewardResult:
    """Score one batch horizon with evaluator det-v1. Pure function of artefacts.

    Ambient memory lessons, journal text, and worker opinions are not inputs
    and cannot change the score. Unavailable and invalid feedback are
    statuses, never zero rewards: the policy must not learn from absence.
    """
    from .rank import question_coverage

    notes: list[str] = []
    if not providers_ok:
        return RewardResult("unavailable", 0.0, {}, ("providers failed; no batch to score",))
    if not isinstance(papers, list):
        return RewardResult("invalid", 0.0, {}, ("papers must be a list",))
    if not isinstance(n_provider_calls, int) or not isinstance(n_model_calls, int):
        return RewardResult("invalid", 0.0, {}, ("call counts must be ints",))
    try:
        coverage = question_coverage(question, papers)
        cover = min(1.0, coverage["best_cover"] / max(coverage["min_cover"], 1))
    except Exception as exc:
        return RewardResult("evaluator-failed", 0.0, {}, (f"coverage failed: {exc}",))
    claims = list(claims or [])
    supported = sum(1 for c in claims if getattr(c, "support", "") == "supported")
    partial = sum(1 for c in claims if getattr(c, "support", "") == "partial")
    if claims:
        grounding = (supported + 0.5 * partial) / len(claims)
    else:
        grounding = 0.0
        notes.append("no claims declared; grounding 0")
    merged = (dedupe_stats or {}).get("merged", 0)
    candidates = (dedupe_stats or {}).get("candidates", len(papers))
    dup_ratio = (merged / candidates) if candidates else 0.0
    spec = REWARD_SPEC
    gain = spec["gain_coverage"] * cover + spec["gain_grounding"] * grounding
    cost = min(1.0, (n_provider_calls + spec["cost_model_weight"] * n_model_calls)
               / spec["cost_scale"])
    redundancy = spec["red_dup"] * dup_ratio + spec["red_uncovered"] * (1.0 - cover)
    reward = spec["w_gain"] * gain - spec["w_cost"] * cost - spec["w_redundancy"] * redundancy
    lo, hi = spec["clip"]
    reward = max(lo, min(hi, reward))
    components = {"coverage": cover, "grounding": grounding, "gain": gain,
                  "cost": cost, "redundancy": redundancy, "reward": reward}
    return RewardResult("ok", reward, components, tuple(notes))


# --- selector session: decide, score, persist ---

def _len(value) -> int:
    try:
        return len(value or [])
    except TypeError:
        return 0

@dataclass
class Decision:
    id: str
    strategy_id: str
    query: str
    fallback: str | None
    distribution: dict
    p_selected: float
    scores: dict
    tie_broken: bool
    features: list
    clipped: bool
    cold_start: bool
    mode: str
    epsilon: float
    uncertainties: dict = field(default_factory=dict)
    decision_ms: float = 0.0


class SelectorSession:
    """One run's selection loop. Owns policy + rolling stats, nothing else.

    No spec, budget, promotion, or evaluator handles cross this boundary:
    budgets arrive as plain remaining-fractions, exactly like the scheduler.
    """

    def __init__(self, config: BanditConfig, journal=None, record_dir=None,
                 run_tag: str = "run") -> None:
        from .journal import emit as _emit

        self.config = config
        self.journal = journal
        self.record_dir = Path(record_dir) if record_dir else None
        self.run_tag = run_tag
        self.records: list[dict] = []
        self.gaps: str = ""
        self.last_coverage = 0.0
        self.last_dup_rate = 0.0
        self.degraded = False
        self.fallback_note: str | None = None
        self._emit = _emit
        self._counter = 0
        self.store: PolicyStore | None = None
        if config.mode in ("learning", "frozen"):
            try:
                assert config.policy_dir is not None
                self.store = PolicyStore(config.policy_dir,
                                         create=(config.mode == "learning"))
                self.rng = self.store.load_rng(config.seed)
            except BanditError as e:
                self._degrade(f"policy store unusable ({e}); fixed policy engaged")
                self.rng = random.Random(config.seed)
        else:
            self.rng = random.Random(config.seed)

    def _degrade(self, reason: str) -> None:
        self.degraded = True
        self.fallback_note = reason
        self._emit(self.journal, "bandit", "fallback", reason[:300])

    def degrade(self, reason: str) -> None:
        """Drop to fixed decisions after a mid-run store failure. Loud."""
        self._degrade(reason)

    def set_gaps(self, gaps: list[str]) -> None:
        self.gaps = "\n".join(gaps[-8:])

    def _features(self, question: str, budget_frac: float) -> tuple[list[float], bool]:
        return extract_features(question, bool(self.gaps.strip()),
                                self.last_coverage, self.last_dup_rate, budget_frac)

    def _record(self, record: dict) -> None:
        self.records.append(record)
        if self.record_dir is not None:
            from . import report as reportmod

            reportmod.record_bandit(self.record_dir, record)

    def _persist_rng(self) -> None:
        if self.store is not None and self.config.mode == "learning" and not self.degraded:
            self.store.save_rng(self.rng)

    def decide(self, question: str, budget_frac: float = 1.0,
               model: str = "unknown") -> Decision:
        """Select a strategy. Records the full distribution, always honestly."""
        from . import report as reportmod

        started = time.perf_counter()
        features, clipped = self._features(question, budget_frac)
        eligible = list(self.config.eligible)
        cold = self.store is None or int(self.store.snapshot.get("updates", 0)) == 0
        use_fixed = self.degraded or self.config.mode in ("off", "fixed")
        if self.config.mode == "off":
            raise BanditError("decide() called with bandit off: wire a mode or no selector")
        if use_fixed:
            chosen = self.config.fixed_strategy
            dist = {sid: (1.0 if sid == chosen else 0.0) for sid in eligible}
            scores = {sid: (1.0 if sid == chosen else 0.0) for sid in eligible}
            tie = False
            uncert: dict = {}
        else:
            assert self.store is not None
            x = np.asarray(features, dtype=float)
            scores = {sid: float(self.store.weights(sid) @ x) for sid in eligible}
            best = max(scores.values())
            tied = [sid for sid in eligible if scores[sid] == best]
            greedy = tied[0]  # deterministic tie-break: lowest eligible index
            tie = len(tied) > 1
            eps = 0.0 if self.config.mode == "frozen" else self.config.epsilon
            if eps > 0.0 and self.rng.random() < eps:
                chosen = eligible[self.rng.randrange(len(eligible))]
            else:
                chosen = greedy
            k = len(eligible)
            dist = {sid: ((1.0 - eps) + eps / k if sid == greedy else eps / k)
                    for sid in eligible}
            uncert = {sid: self.store.uncertainty(sid, features) for sid in eligible}
        query, fallback = build_query(chosen, question, self.gaps)
        if not query.strip():
            query, fallback = question, f"{chosen} fell back: empty query"
        if (self.store is not None and not self.degraded
                and self.config.mode == "learning"):
            try:
                did = self.store.next_decision_id(self.run_tag)
            except BanditError as e:
                self._degrade(f"decision counter unwritable ({e}); fixed policy engaged")
                did = f"{self.run_tag}:d{self._counter:06d}"
                self._counter += 1
        else:
            # Fixed and frozen sessions never mutate the snapshot.
            did = f"{self.run_tag}:d{self._counter:06d}"
            self._counter += 1
        self._persist_rng()
        decision = Decision(
            id=did, strategy_id=chosen, query=query, fallback=fallback or self.fallback_note,
            distribution=dist, p_selected=dist[chosen], scores=scores, tie_broken=tie,
            features=features, clipped=clipped, cold_start=cold, mode=self.config.mode,
            epsilon=0.0 if self.config.mode in ("fixed", "frozen") else self.config.epsilon,
            uncertainties=uncert, decision_ms=(time.perf_counter() - started) * 1000.0)
        try:
            code_revision = reportmod.code_revision()
        except OSError:
            code_revision = "unknown"
        self._record({"v": RECORD_VERSION, "type": "decision", "id": did,
                      "run_id": self.run_tag, "at": _utcnow(),
                      "question": question[:500], "gaps_open": bool(self.gaps.strip()),
                      "features": features, "feature_version": FEATURE_VERSION,
                      "eligible": eligible, "distribution": dist, "selected": chosen,
                      "p_selected": decision.p_selected, "scores": scores,
                      "tie_broken": tie, "uncertainties": uncert,
                      "mode": decision.mode, "epsilon": decision.epsilon,
                      "policy_impl": POLICY_IMPL, "rng_version": RNG_VERSION,
                      "seed": self.config.seed,
                      "strategy_versions": {sid: STRATEGY_VERSION for sid in eligible},
                      "code_revision": code_revision, "model": model,
                      "reward_version": REWARD_VERSION, "evaluator": EVALUATOR_ID,
                      "query": query[:MAX_QUERY_CHARS], "fallback": decision.fallback,
                      "cold_start": cold, "clipped": clipped,
                      "degraded": self.degraded, "decision_ms": decision.decision_ms})
        self._emit(self.journal, "bandit", "decided",
                   f"{did}: {chosen} p={decision.p_selected:.3f} mode={decision.mode}")
        return decision

    def score_and_observe(self, decision: Decision, outcome: dict) -> dict:
        """Score one batch horizon; learning mode applies eligible observations."""
        started = time.perf_counter()
        safe = outcome if isinstance(outcome, dict) else {}
        try:
            result = score_reward(
                safe.get("question", ""), safe.get("papers", []),
                safe.get("claims", ()), safe.get("validation"),
                safe.get("dedupe"), safe.get("n_provider_calls", 0),
                safe.get("n_model_calls", 0), safe.get("providers_ok", False))
        except Exception as exc:
            result = RewardResult("evaluator-failed", 0.0, {},
                                  (f"evaluator raised: {exc}",))
        update: dict = {"applied": False, "update_id": None, "reason": "mode does not learn"}
        if result.status == "ok":
            self.last_coverage = result.components["coverage"]
            self.last_dup_rate = min(1.0, max(0.0, float(
                (safe.get("dedupe") or {}).get("merged", 0))
                / max((safe.get("dedupe") or {}).get("candidates", 0)
                        or len(safe.get("papers", []) or [1]), 1)))
        obs = {"id": decision.id, "decision_id": decision.id, "status": result.status,
               "strategy_id": decision.strategy_id,
               "strategy_version": STRATEGY_VERSION,
               "features": decision.features, "reward": result.reward,
               "components": result.components, "notes": list(result.notes),
               "reward_version": REWARD_VERSION, "evaluator": EVALUATOR_ID,
               "counts": {"provider_calls": safe.get("n_provider_calls", 0),
                          "model_calls": safe.get("n_model_calls", 0),
                          "papers": _len(safe.get("papers", [])),
                          "claims": _len(safe.get("claims", []))}}
        if (self.config.mode == "learning" and not self.degraded
                and self.store is not None):
            try:
                applied = self.store.observe(obs)
            except BanditError as e:
                self._degrade(f"observation store failed ({e}); fixed policy engaged")
                applied = {"outcome": "store-failed", "note": str(e)[:200]}
            update = {"applied": applied["outcome"] == "applied",
                      "update_id": (self.store.snapshot.get("updates")
                                    if applied["outcome"] == "applied" else None),
                      "reason": f"{applied['outcome']}: {applied['note']}"}
        elif self.config.mode == "learning" and self.degraded:
            update = {"applied": False, "update_id": None, "reason": "degraded: fixed fallback"}
        record = {"v": RECORD_VERSION, "type": "observation", **obs, "update": update,
                  "at": _utcnow(), "run_id": self.run_tag,
                  "observe_ms": (time.perf_counter() - started) * 1000.0}
        self._record(record)
        self._emit(self.journal, "bandit", "scored",
                   f"{decision.id}: {result.status} r={result.reward:.3f} "
                   f"update={update['reason']}"[:300])
        return record


# --- offline replay: counterfactuals from logged bandit data ---

def counterfactual_ips(decisions: list[dict], observations: list[dict],
                       policy_fn) -> dict:
    """Inverse-propensity estimate of a counterfactual policy from logs.

    policy_fn(features) -> strategy_id replays what the candidate WOULD
    have chosen per logged context. Only logged selections matching the
    counterfactual choice contribute, weighted by 1/p_selected. Rewards
    come from joined observations (status ok only); nothing is re-executed.
    Returns the estimate plus the effective (matched) sample size.
    """
    rewards = {o.get("decision_id"): o for o in observations if o.get("status") == "ok"}
    terms: list[float] = []
    for record in decisions:
        obs = rewards.get(record.get("id"))
        if obs is None:
            continue
        try:
            imagined = policy_fn(record.get("features", []))
        except Exception:
            continue  # candidate policy abstains: excluded, never zero-filled
        if imagined != record.get("selected"):
            continue
        prob = record.get("p_selected", 0.0)
        if not isinstance(prob, (int, float)) or prob <= 0.0:
            continue
        terms.append(float(obs["reward"]) / prob)
    if not terms:
        return {"ips": 0.0, "matched": 0, "total": len(decisions)}
    return {"ips": sum(terms) / len(decisions), "matched": len(terms),
            "total": len(decisions)}


# --- controlled comparison on scripted fixtures (mechanics, not benefit) ---

COMPARISON_VERSION = 1

# Fixture assumption (documented, not hidden): the scripted model grounds
# its answer iff the batch carries evidence (titles containing "Study on
# X"). This simulates "good evidence enables grounded answers" so the
# SELECTOR mechanics are exercised; it proves nothing about model quality
# or real research benefit. Sample size (4 dev + 2 held-out) is fixed by
# fixture budget and is far below any inferential bar by design.
FIXTURE_MODEL_MARKER = "Study on X"


@dataclass(frozen=True)
class FixtureQuestion:
    id: str
    family: str
    question: str
    gaps: tuple[str, ...] = ()


def fixture_questions() -> tuple[list[FixtureQuestion], list[FixtureQuestion]]:
    # Precise questions carry keywords so strategies produce distinct
    # queries; keyword-less questions would collapse every strategy onto
    # one query (honest, but uninformative for selection mechanics).
    dev = [
        FixtureQuestion("d1", "broad", "what are the effects of X on health?"),
        FixtureQuestion("d2", "jargon", "what is the mortality risk overview?"),
        FixtureQuestion("d3", "followup", "treatment options for X?",
                        ("iter1/c1: dosage unknown (unsupported)",)),
        FixtureQuestion("d4", "precise", "X mortality outcomes study"),
    ]
    held = [
        FixtureQuestion("h1", "broad", "what are the effects of Y on health?"),
        FixtureQuestion("h2", "precise", "Y mortality outcomes study"),
    ]
    return dev, held


def fixture_papers(fixture: FixtureQuestion, strategy_id: str) -> list[dict]:
    """Deterministic provider double: per-question, per-strategy batches.

    Mixed by design: no arm dominates every state, so the fixed policy
    stays competitive and the fixture cannot rig a learning win.
    """
    strong = [{"title": f"Study on X: {fixture.question[:60]}",
               "abstract": f"{fixture.question} evidence with measured outcomes."},
              {"title": f"Study on X replication of {fixture.question[:40]}",
               "abstract": f"Independent replication measuring {fixture.question}."}]
    weak = [{"title": "Unrelated widgets quarterly",
             "abstract": "A survey of widget manufacturing with no medical content."}]
    table = {
        # strategy that fetches the strong batch per question
        "d1": "focused-v1",
        "d2": "terms-v1",
        "d3": "gap-v1",
        "d4": "original-v1",
        "h1": "focused-v1",
        "h2": "original-v1",
    }
    if strategy_id == table[fixture.id]:
        return strong
    if fixture.id == "d2" and strategy_id == "focused-v1":
        return []  # genuine zero-utility horizon (retry also misses)
    return weak


class FixtureModel:
    """Scripted model double: grounds iff the batch carries evidence."""

    model = "fixture-model"

    def complete(self, system: str, user: str, max_tokens: int = 8000) -> str:
        if FIXTURE_MODEL_MARKER in user:
            return ("Evidence [1] [2].\n```claims\n" + json.dumps([{
                "id": "c1", "text": "X shows measured effects",
                "support": "supported",
                "evidence": [{"paper": 1, "span": FIXTURE_MODEL_MARKER}]}]) + "\n```")
        return "Brief [1]."


def fixture_http(fixture: FixtureQuestion):
    """Mock literature transport keyed by query string, like real providers.

    Same query string always yields the same batch: when two strategies
    produce identical queries (gap degrading to focused without gaps,
    terms falling back without table hits), the first producer in
    STRATEGY_IDS order defines the batch for both. Unknown queries get a
    weak default instead of crashing the double.
    """
    import httpx

    batches: dict[str, list[dict]] = {}
    for sid in STRATEGY_IDS:
        query, _ = build_query(sid, fixture.question, "\n".join(fixture.gaps))
        batches.setdefault(query, fixture_papers(fixture, sid))

    def handler(req) -> "httpx.Response":
        url = str(req.url)
        if "openalex" in url:
            params = dict(req.url.params)
            query = params.get("search", "")
            batch = batches.get(query)
            if batch is None:
                batch = [{"title": "Unrelated widgets quarterly",
                          "abstract": "No medical content for this unknown query."}]
            results = []
            for i, paper in enumerate(batch):
                words = paper["abstract"].split()
                results.append({
                    "id": f"W{i}", "title": paper["title"],
                    "doi": f"https://doi.org/10.9/{fixture.id}-{sid}-{i}",
                    "publication_year": 2023, "cited_by_count": 3,
                    "authorships": [{"author": {"display_name": "A. Uthor"}}],
                    "primary_location": {"source": {"display_name": "J X"}},
                    "abstract_inverted_index": {w.strip(".,:?"): [j]
                                                for j, w in enumerate(words)}})
            return httpx.Response(200, json={"results": results})
        return httpx.Response(200, json={"data": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


def run_fixture_question(fixture: FixtureQuestion, selector: SelectorSession,
                         journal, out_dir: Path, model=None) -> dict:
    """One question through the real retrieval+assessment path. Deterministic."""
    from .cycle import NoEvidence, run_review

    out_dir.mkdir(parents=True, exist_ok=True)
    model = model or FixtureModel()
    selector.set_gaps(list(fixture.gaps))
    http = fixture_http(fixture)
    row: dict = {"id": fixture.id, "family": fixture.family}
    started = time.perf_counter()
    try:
        iteration = run_review(fixture.question, 5, http, model, journal=journal,
                               record_dir=str(out_dir), selector=selector)
        row["completed"] = True
        row["n_claims"] = len(iteration.provenance["claims"])
    except NoEvidence:
        # Genuine zero-utility horizon: run_review scored it before raising.
        row["completed"] = False
        row["n_claims"] = 0
        row["no_evidence"] = True
    decision_rec = next(r for r in reversed(selector.records)
                        if r.get("type") == "decision" and r.get("question") == fixture.question)
    obs_rec = next(r for r in reversed(selector.records)
                   if r.get("type") == "observation" and r.get("decision_id") == decision_rec["id"])
    row["decision_id"] = decision_rec["id"]
    row["strategy"] = decision_rec["selected"]
    row["p_selected"] = decision_rec["p_selected"]
    row["reward"] = obs_rec["reward"]
    row["status"] = obs_rec["status"]
    row["components"] = obs_rec["components"]
    row["counts"] = obs_rec["counts"]
    row["decision_ms"] = decision_rec["decision_ms"]
    row["fallback"] = decision_rec.get("fallback")
    row["wall_ms"] = (time.perf_counter() - started) * 1000.0
    return row


def _dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def compare_three_arm(out_dir: str | Path, seed: int = 7, epsilon: float = 0.2) -> dict:
    """A/B/C on scripted fixtures: fixed / fixed+memory / bandit+memory.

    Arms B and C do identical memory work; only C selects via the bandit,
    so the B/C difference isolates the selector. Held-out questions run
    FROZEN: their rewards never feed the policy. Fixture mechanics only —
    the report carries its own inconclusive-for-benefit disclaimer.
    """
    from .journal import Journal
    from .memory import Memory

    out = Path(out_dir)
    dev, held = fixture_questions()
    report: dict = {
        "comparison_version": COMPARISON_VERSION,
        "seed": seed,
        "epsilon": epsilon,
        "reward_version": REWARD_VERSION,
        "reward_spec": REWARD_SPEC,
        "strategy_version": STRATEGY_VERSION,
        "policy_impl": POLICY_IMPL,
        "evaluator": EVALUATOR_ID,
        "arms": {"A": "fixed original-v1, no memory",
                 "B": "fixed original-v1 with assessment memory",
                 "C": "bandit selector with assessment memory"},
        "dev_ids": [q.id for q in dev],
        "held_ids": [q.id for q in held],
        "sample_rationale": ("4 dev + 2 held-out scripted questions: enough to "
                             "exercise mechanics (updates, freezing, costs), "
                             "far below any bar for real-benefit inference."),
    }

    def run_arm(name: str, questions: list[FixtureQuestion], mode: str,
                policy_dir: Path | None, with_memory: bool,
                memory: Memory | None) -> dict:
        journal = Journal()
        selector = SelectorSession(
            BanditConfig(mode=mode, epsilon=epsilon, seed=seed,
                         policy_dir=str(policy_dir) if policy_dir else None),
            journal=journal, record_dir=None, run_tag=f"{name}")
        rows = []
        for fixture in questions:
            qdir = out / name / fixture.id
            row = run_fixture_question(fixture, selector, journal, qdir)
            rows.append(row)
            if with_memory and memory is not None:
                memory.record(
                    text=f"{fixture.id} {row['strategy']}: r={row['reward']:.3f} "
                         f"status={row['status']}",
                    target="", kept=(row["status"] == "ok" and row["reward"] > 0.0),
                    assessment_id=row.get("strategy", ""), code_revision="fixture")
        if with_memory and memory is not None:
            memory.save()
        costs = {"model_calls": sum(r["counts"]["model_calls"] for r in rows),
                 "provider_calls": sum(r["counts"]["provider_calls"] for r in rows),
                 "decision_ms_total": sum(r["decision_ms"] for r in rows)}
        return {"rows": rows, "costs": costs,
                "journal_events": [(n.phase, n.event) for n in journal.notes]}

    mem_b = Memory(out / "arm-B" / "memory.jsonl")
    mem_c = Memory(out / "arm-C" / "memory.jsonl")
    arm_a_dev = run_arm("arm-A-dev", dev, "fixed", None, False, None)
    arm_b_dev = run_arm("arm-B-dev", dev, "fixed", None, True, mem_b)
    arm_c_dev = run_arm("arm-C-dev", dev, "learning", out / "policy-C", True, mem_c)
    arm_a_held = run_arm("arm-A-held", held, "fixed", None, False, None)
    arm_b_held = run_arm("arm-B-held", held, "fixed", None, True, mem_b)
    arm_c_held = run_arm("arm-C-held", held, "frozen", out / "policy-C", True, mem_c)
    mem_b.save()
    mem_c.save()

    def held_summary(arm: dict) -> dict:
        rewards = [r["reward"] for r in arm["rows"]]
        return {"rewards": rewards,
                "mean": sum(rewards) / len(rewards) if rewards else 0.0,
                "range": [min(rewards), max(rewards)] if rewards else [0.0, 0.0],
                "strategies": [r["strategy"] for r in arm["rows"]]}

    policy_bytes = _dir_bytes(out / "policy-C")
    report["dev"] = {"A": arm_a_dev, "B": arm_b_dev, "C": arm_c_dev}
    report["held"] = {"A": arm_a_held, "B": arm_b_held, "C": arm_c_held}
    report["held_summary"] = {k: held_summary(v) for k, v in report["held"].items()}
    report["costs"] = {
        "A": {**arm_a_dev["costs"], **{f"held_{k}": v for k, v in arm_a_held["costs"].items()}},
        "B": {**arm_b_dev["costs"], **{f"held_{k}": v for k, v in arm_b_held["costs"].items()}},
        "C": {**arm_c_dev["costs"], **{f"held_{k}": v for k, v in arm_c_held["costs"].items()},
              "policy_bytes": policy_bytes,
              "memory_bytes": _dir_bytes(out / "arm-C")},
    }
    report["disclaimers"] = [
        "Scripted fixtures prove mechanics (updates apply, freezing holds, "
        "costs are counted, held-out runs frozen). They are not evidence of "
        "real research benefit.",
        "Held-out N=2: per-question rewards are reported raw with range; no "
        "confidence intervals are computed or implied.",
        "The fixture model grounds iff the batch carries evidence, by "
        "construction. Selector signal, not model quality, is under test.",
    ]
    return report
