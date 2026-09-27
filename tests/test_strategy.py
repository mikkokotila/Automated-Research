"""Build ext (#19): measured retrieval-strategy selection."""

import json
import math
from pathlib import Path

import pytest

from canary import cycle as cyclemod
from canary import report as reportmod
from canary.journal import Journal
from canary.spec import RunBudget, RunSpec
from canary.strategy import (
    EPS_MAX, FEATURE_DIM, POLICY_IMPL, RECORD_VERSION, REWARD_SPEC,
    REWARD_VERSION, STRATEGY_IDS, STRATEGY_VERSION, BanditConfig, BanditError,
    FixtureModel, PolicyCorrupt, PolicyStore, SelectorSession, build_query,
    compare_three_arm, extract_features, fixture_http, fixture_papers,
    fixture_questions, run_fixture_question, score_reward,
)
from tests.test_milestone4 import ScriptedMuse, grounded_review, mock_http


def papers(*titles):
    from canary.papers import Paper

    out = []
    for i, title in enumerate(titles, 1):
        out.append(Paper(ref=f"[{i}]", title=title, doi=f"10.9/x{i}", year=2023,
                         authors=("A. Uthor",), source="J X", citations=3,
                         abstract=f"{title} with measured outcomes.",
                         evidence="abstract", oa_url="", license="",
                         identifiers={"doi": f"10.9/x{i}"}))
    return out


def claims(*supports):
    from canary.synthesize import Claim

    return tuple(Claim(id=f"c{i}", text=f"claim {i}", support=s, evidence=(),
                       uncertainty="", scope="")
                 for i, s in enumerate(supports, 1))


# --- strategies ---


def test_build_query_original_focused_terms_gap():
    q, fb = build_query("original-v1", "Why is X red?")
    assert (q, fb) == ("Why is X red?", None)
    q, fb = build_query("focused-v1", "Why is X red?")
    assert fb is None and q.strip() and q != "Why is X red?"
    q, fb = build_query("terms-v1", "what is the mortality risk overview?")
    assert fb is None and "death" in q and "hazard" in q
    assert len(q.split()) - len("what is the mortality risk overview?".split()) <= 4
    q, fb = build_query("terms-v1", "Why is X red?")
    assert q == "Why is X red?" and fb and "no table hits" in fb
    q, fb = build_query("gap-v1", "Why is X red?", "dosage unknown (unsupported)")
    assert fb is None and "dosage" in q
    q, fb = build_query("gap-v1", "Why is X red?")
    assert fb and "no open gaps" in fb
    with pytest.raises(ValueError, match="unknown strategy"):
        build_query("mythic-v9", "Why is X red?")


def test_build_query_never_empty_and_bounded():
    for sid in STRATEGY_IDS:
        q, _ = build_query(sid, "X?")
        assert q.strip() and len(q) <= 500
    long_q = "mortality risk " * 200
    q, _ = build_query("terms-v1", long_q)
    assert len(q) <= 500


# --- features ---


def test_extract_features_preaction_only_and_clipped():
    vec, clipped = extract_features("seed question about X?", gaps_open=True,
                                    prior_coverage=0.5, prior_dup_rate=0.2,
                                    budget_frac=0.8)
    assert len(vec) == FEATURE_DIM and not clipped
    assert vec[2] == 1.0 and vec[3] == 0.5 and vec[5] == 0.8
    vec, clipped = extract_features("q?", prior_coverage=float("nan"),
                                    prior_dup_rate=9.0, budget_frac=-1.0)
    assert clipped and vec == [vec[0], vec[1], 0.0, 0.0, 1.0, 0.0]
    assert all(math.isfinite(v) for v in vec)


# --- config ---


def test_bandit_config_validation_and_roundtrip():
    with pytest.raises(ValueError, match="mode"):
        BanditConfig(mode="vibes")
    with pytest.raises(ValueError, match="epsilon"):
        BanditConfig(mode="learning", epsilon=EPS_MAX + 0.1, policy_dir="p")
    with pytest.raises(ValueError, match="seed"):
        BanditConfig(mode="fixed", seed=-1)
    with pytest.raises(ValueError, match="policy_dir"):
        BanditConfig(mode="learning")
    with pytest.raises(ValueError, match="eligible"):
        BanditConfig(mode="fixed", eligible=())
    with pytest.raises(ValueError, match="eligible"):
        BanditConfig(mode="fixed", fixed_strategy="gap-v1", eligible=("original-v1",))
    cfg = BanditConfig(mode="learning", epsilon=0.2, seed=3, policy_dir="/tmp/p",
                       eligible=("original-v1", "focused-v1"))
    assert BanditConfig.from_dict(json.loads(json.dumps(cfg.to_dict()))) == cfg


# --- policy store ---


def _obs(obs_id="d1", arm="original-v1", reward=0.5, status="ok"):
    return {"id": obs_id, "strategy_id": arm, "status": status,
            "strategy_version": STRATEGY_VERSION,
            "features": [0.1] * FEATURE_DIM, "reward": reward}


def test_store_roundtrip_is_byte_identical(tmp_path):
    store = PolicyStore(tmp_path / "pol")
    store.observe(_obs("a"))
    before = (tmp_path / "pol" / "snapshot.json").read_bytes()
    PolicyStore(tmp_path / "pol")
    after = (tmp_path / "pol" / "snapshot.json").read_bytes()
    assert before == after
    assert (tmp_path / "pol" / "snapshot.bak.json").is_file()


def test_observe_applies_once_and_rejects_garbage(tmp_path):
    store = PolicyStore(tmp_path / "pol")
    assert store.observe(_obs("a"))["outcome"] == "applied"
    assert store.snapshot["updates"] == 1
    w1 = store.weights("original-v1").copy()
    assert store.observe(_obs("a"))["outcome"] == "duplicate"
    assert store.snapshot["updates"] == 1
    assert (store.weights("original-v1") == w1).all()
    for bad, why in [({}, "id"), ({"id": "x", "strategy_id": "nope", "status": "ok",
                                   "features": [0.0] * FEATURE_DIM, "reward": 0.1}, "arm"),
                     (_obs("b", reward=float("nan")), "finite"),
                     (_obs("c", reward=0.1) | {"features": [0.0]}, "features")]:
        assert why in store.observe(bad)["note"]
    assert store.snapshot["updates"] == 1
    rec = store.observe(_obs("d", status="unavailable"))
    assert rec["outcome"] == "recorded" and store.snapshot["updates"] == 1
    assert "d" not in store.snapshot["applied_ids"]


def test_store_resets_arm_on_strategy_version_change(tmp_path):
    store = PolicyStore(tmp_path / "pol")
    store.observe(_obs("a"))
    assert store.snapshot["arms"]["original-v1"]["n"] == 1
    snap = json.loads((tmp_path / "pol" / "snapshot.json").read_text())
    snap["arms"]["original-v1"]["strategy_version"] = 0
    (tmp_path / "pol" / "snapshot.json").write_text(json.dumps(snap))
    store2 = PolicyStore(tmp_path / "pol")
    out = store2.observe(_obs("b"))
    assert out["outcome"] == "applied" and "reset to prior" in out["note"]
    assert store2.snapshot["arms"]["original-v1"]["n"] == 1


def test_store_rejects_corrupt_snapshots(tmp_path):
    root = tmp_path / "pol"
    root.mkdir()
    (root / "snapshot.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(PolicyCorrupt, match="unreadable"):
        PolicyStore(root)
    snap = {"snapshot_version": 999, "policy_impl": POLICY_IMPL, "arms": {},
            "applied_ids": [], "updates": 0}
    (root / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
    with pytest.raises(PolicyCorrupt, match="not 1"):
        PolicyStore(root)
    store = PolicyStore(tmp_path / "fresh")
    good = json.loads((tmp_path / "fresh" / "snapshot.json").read_text())
    good["arms"]["original-v1"]["b"] = [0.0]  # wrong dims
    (root / "snapshot.json").write_text(json.dumps(good), encoding="utf-8")
    with pytest.raises(PolicyCorrupt, match="dimensions"):
        PolicyStore(root)


def test_rng_state_roundtrip_reproduces_draws(tmp_path):
    store = PolicyStore(tmp_path / "pol")
    rng = store.load_rng(11)
    assert [rng.random() for _ in range(3)] == [random_draws(11)[i] for i in range(3)]
    store.save_rng(rng)
    resumed = PolicyStore(tmp_path / "pol").load_rng(11)
    assert [resumed.random() for _ in range(3)] == [random_draws(11)[i] for i in range(3, 6)]


def random_draws(seed):
    import random

    rng = random.Random(seed)
    return [rng.random() for _ in range(8)]


# --- selector ---


def test_fixed_mode_is_one_hot_and_writes_nothing(tmp_path):
    journal = Journal()
    session = SelectorSession(BanditConfig(mode="fixed"), journal=journal,
                              record_dir=tmp_path / "rec", run_tag="t")
    first = session.decide("Why is X red?")
    second = session.decide("Why is X red?")
    assert (first.strategy_id, second.strategy_id) == ("original-v1",) * 2
    assert first.distribution == {"original-v1": 1.0, "focused-v1": 0.0,
                                  "terms-v1": 0.0, "gap-v1": 0.0}
    assert first.epsilon == 0.0 and first.uncertainties == {}
    assert (tmp_path / "rec" / "bandit.jsonl").is_file()
    with pytest.raises(BanditError, match="off"):
        SelectorSession(BanditConfig(mode="off")).decide("q?")


def test_learning_distribution_and_deterministic_ties(tmp_path):
    session = SelectorSession(BanditConfig(mode="learning", epsilon=0.2, seed=5,
                                           policy_dir=str(tmp_path / "pol")))
    first = session.decide("Why is X red?")
    assert first.tie_broken  # fresh prior: all arms score zero
    k, eps = 4, 0.2
    greedy = min(s for s in STRATEGY_IDS
                 if first.distribution[s] == max(first.distribution.values()))
    assert first.distribution[greedy] == pytest.approx((1 - eps) + eps / k)
    others = [p for s, p in first.distribution.items() if s != greedy]
    assert others == [pytest.approx(eps / k)] * (k - 1)
    assert abs(sum(first.distribution.values()) - 1.0) < 1e-9


def test_learning_frequencies_match_mixture(tmp_path):
    from collections import Counter

    session = SelectorSession(BanditConfig(mode="learning", epsilon=0.2, seed=9,
                                           policy_dir=str(tmp_path / "pol")))
    picks = Counter(session.decide(f"question {i} about X?").strategy_id for i in range(2000))
    # Fresh prior ties break to original-v1; mixture predicts its frequency.
    expected = (1 - 0.2) + 0.2 / 4
    assert abs(picks["original-v1"] / 2000 - expected) < 0.03
    for sid in ("focused-v1", "terms-v1", "gap-v1"):
        assert abs(picks[sid] / 2000 - 0.2 / 4) < 0.03


def test_frozen_is_greedy_and_never_writes(tmp_path):
    store = PolicyStore(tmp_path / "pol")
    store.observe(_obs("a", arm="focused-v1", reward=0.9))
    before = (tmp_path / "pol" / "snapshot.json").read_bytes()
    session = SelectorSession(BanditConfig(mode="frozen", seed=1,
                                           policy_dir=str(tmp_path / "pol")))
    first = session.decide("Why is X red?")
    assert first.epsilon == 0.0 and not first.cold_start
    assert first.strategy_id == "focused-v1"  # learned win, greedy replay
    assert (tmp_path / "pol" / "snapshot.json").read_bytes() == before


def test_frozen_missing_snapshot_falls_back_loudly(tmp_path):
    journal = Journal()
    session = SelectorSession(BanditConfig(mode="frozen", seed=1,
                                           policy_dir=str(tmp_path / "pol")),
                              journal=journal)
    assert session.degraded
    decision = session.decide("Why is X red?")
    assert decision.strategy_id == "original-v1" and decision.fallback
    assert any(n.event == "fallback" for n in journal.notes)


def test_corrupt_store_falls_back_to_fixed(tmp_path):
    root = tmp_path / "pol"
    root.mkdir()
    (root / "snapshot.json").write_text("{oops", encoding="utf-8")
    journal = Journal()
    session = SelectorSession(BanditConfig(mode="learning", seed=1,
                                           policy_dir=str(root)), journal=journal)
    assert session.degraded
    assert session.decide("Why is X red?").strategy_id == "original-v1"
    assert any(n.event == "fallback" for n in journal.notes)


def test_decision_record_carries_full_provenance(tmp_path):
    session = SelectorSession(BanditConfig(mode="learning", epsilon=0.2, seed=2,
                                           policy_dir=str(tmp_path / "pol")),
                              record_dir=tmp_path / "rec", run_tag="run-9")
    decision = session.decide("Why is X red?", budget_frac=0.5, model="m-x")
    record = session.records[-1]
    for key in ("v", "id", "run_id", "question", "features", "feature_version",
                "eligible", "distribution", "selected", "p_selected", "scores",
                "tie_broken", "mode", "epsilon", "policy_impl", "rng_version",
                "seed", "strategy_versions", "code_revision", "model",
                "reward_version", "evaluator", "query", "cold_start",
                "decision_ms"):
        assert key in record, key
    assert record["v"] == RECORD_VERSION and record["policy_impl"] == POLICY_IMPL
    assert record["model"] == "m-x" and decision.p_selected == record["p_selected"]
    assert json.loads((tmp_path / "rec" / "bandit.jsonl").read_text())["id"] == decision.id


def test_observation_record_and_update_accounting(tmp_path):
    session = SelectorSession(BanditConfig(mode="learning", epsilon=0.0, seed=2,
                                           policy_dir=str(tmp_path / "pol")))
    decision = session.decide("Does X matter?")
    obs = session.score_and_observe(decision, {
        "question": "Does X matter?",
        "papers": papers("Study on X", "More on X"),
        "claims": claims("supported", "partial"),
        "validation": {}, "dedupe": {"candidates": 2, "kept": 2, "merged": 0},
        "n_provider_calls": 2, "n_model_calls": 1, "providers_ok": True})
    assert obs["status"] == "ok" and obs["update"]["applied"] is True
    assert obs["update"]["update_id"] == 1
    assert obs["components"]["reward"] == pytest.approx(obs["reward"])
    assert session.store.snapshot["applied_ids"] == [decision.id]


def test_unavailable_feedback_never_updates(tmp_path):
    session = SelectorSession(BanditConfig(mode="learning", seed=2,
                                           policy_dir=str(tmp_path / "pol")))
    decision = session.decide("Does X matter?")
    obs = session.score_and_observe(decision, {
        "question": "Does X matter?", "papers": [], "claims": (),
        "validation": {}, "dedupe": {"candidates": 0, "kept": 0, "merged": 0},
        "n_provider_calls": 2, "n_model_calls": 0, "providers_ok": False})
    assert obs["status"] == "unavailable" and obs["update"]["applied"] is False
    assert session.store.snapshot["updates"] == 0
    assert session.store.snapshot["applied_ids"] == []


def test_midrun_store_failure_degrades_to_fixed(tmp_path):
    session = SelectorSession(BanditConfig(mode="learning", seed=2,
                                           policy_dir=str(tmp_path / "pol")))
    decision = session.decide("Does X matter?")
    (tmp_path / "pol").chmod(0o555)  # real I/O failure: nothing writable
    try:
        obs = session.score_and_observe(decision, {
            "question": "Does X matter?", "papers": papers("Study on X"),
            "claims": claims("supported"), "validation": {},
            "dedupe": {"candidates": 1, "kept": 1, "merged": 0},
            "n_provider_calls": 2, "n_model_calls": 1, "providers_ok": True})
    finally:
        (tmp_path / "pol").chmod(0o755)
    assert session.degraded and obs["update"]["reason"].startswith("store-failed")
    assert session.decide("next?").strategy_id == "original-v1"


# --- reward ---


def _score(question="Does X matter?", titles=("Matter and X outcomes",),
           supports=("supported",), merged=0, providers_ok=True, calls=(2, 1)):
    return score_reward(question, papers(*titles), claims(*supports), {},
                        {"candidates": len(titles), "kept": len(titles), "merged": merged},
                        calls[0], calls[1], providers_ok)


def test_reward_math_matches_predeclared_spec():
    assert REWARD_SPEC["version"] == REWARD_VERSION
    result = _score()
    assert result.status == "ok"
    c = result.components
    # coverage 1.0 (adequate), grounding 1.0, cost (2+2*1)/10, redundancy (1-1)*0.5
    assert c["coverage"] == 1.0 and c["grounding"] == 1.0
    assert c["cost"] == pytest.approx(0.4) and c["redundancy"] == 0.0
    assert c["reward"] == pytest.approx(1.0 - 0.5 * 0.4)


def test_reward_ignores_finding_direction():
    from canary.synthesize import Claim

    def supported(text):
        return (Claim(id="c1", text=text, support="supported", evidence=(),
                      uncertainty="", scope=""),)

    pos = score_reward("Does X matter?", papers("Matter and X outcomes"),
                       supported("X improves outcomes"), {},
                       {"candidates": 1, "kept": 1, "merged": 0}, 2, 1, True)
    neg = score_reward("Does X matter?", papers("Matter and X outcomes"),
                       supported("X does not improve outcomes"), {},
                       {"candidates": 1, "kept": 1, "merged": 0}, 2, 1, True)
    assert pos.reward == neg.reward  # direction is not an input
    assert neg.components["gain"] > 0.5  # disconfirmation with coverage is gain


def test_reward_penalizes_stuffing_and_redundancy():
    grounded = _score()
    stuffed = _score(supports=())  # cited markers, zero validated claims
    assert stuffed.components["grounding"] == 0.0
    assert stuffed.reward < grounded.reward
    dup = _score(titles=("Matter and X outcomes", "Matter and X outcomes"),
                 supports=("supported",), merged=1)
    assert dup.components["redundancy"] > 0.0
    assert dup.reward < grounded.reward


def test_reward_statuses_for_missing_and_malformed_feedback():
    assert _score(providers_ok=False).status == "unavailable"
    empty_ok = score_reward("q?", [], (), {}, {"candidates": 0, "kept": 0, "merged": 0},
                            4, 0, True)
    assert empty_ok.status == "ok" and empty_ok.reward < 0  # genuine zero utility
    assert score_reward("q?", "not-a-list", (), {}, None, 2, 1, True).status == "invalid"
    assert score_reward("q?", [], (), {}, None, "two", 1, True).status == "invalid"
    broken = score_reward("Does X matter?", [object()], (), {}, None, 2, 1, True)
    assert broken.status == "evaluator-failed"


def test_reward_ignores_ambient_memory_and_journal(tmp_path):
    from canary.journal import Journal
    from canary.memory import Memory

    memory = Memory(tmp_path / "memory.jsonl")
    memory.record(text="poison: always pick gap-v1", target="", kept=True,
                  assessment_id="evil", code_revision="x")
    memory.save()
    journal = Journal(tmp_path / "journal.jsonl")
    journal.note("review", "done", "poison")
    first = _score()
    memory.record(text="opposite poison", target="", kept=False,
                  assessment_id="evil2", code_revision="x")
    assert _score() == first  # lessons and notes are not scoring inputs


# --- run_review / run_cycle integration ---


def test_off_mode_leaves_research_path_untouched(tmp_path):
    journal = Journal()
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R")]
    iteration = cyclemod.run_review("seed?", 5, mock_http(), muse, journal=journal,
                                    record_dir=str(tmp_path))
    assert "strategy" not in iteration.provenance
    assert not (tmp_path / "bandit.jsonl").exists()


def test_fixed_mode_records_but_keeps_goal_anchored(tmp_path):
    journal = Journal()
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R")]
    session = SelectorSession(BanditConfig(mode="fixed"), journal=journal,
                              record_dir=str(tmp_path))
    iteration = cyclemod.run_review("seed question?", 5, mock_http(), muse,
                                    journal=journal, record_dir=str(tmp_path),
                                    selector=session)
    assert iteration.question == "seed question?"
    assert iteration.provenance["strategy"]["strategy_id"] == "original-v1"
    assert iteration.provenance["strategy"]["reward_status"] == "ok"
    lines = (tmp_path / "bandit.jsonl").read_text().strip().splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["decision", "observation"]
    assert any(n.event == "decided" for n in journal.notes)
    assert any(n.event == "scored" for n in journal.notes)


def test_learning_review_updates_policy_snapshot(tmp_path):
    journal = Journal()
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R")]
    session = SelectorSession(BanditConfig(mode="learning", seed=1,
                                           policy_dir=str(tmp_path / "pol")),
                              journal=journal, record_dir=str(tmp_path))
    cyclemod.run_review("seed?", 5, mock_http(), muse, journal=journal,
                        record_dir=str(tmp_path), selector=session)
    assert session.store.snapshot["updates"] == 1
    assert len(session.store.snapshot["applied_ids"]) == 1


def test_empty_horizon_scored_before_no_evidence(tmp_path):
    import httpx

    from canary.cycle import NoEvidence

    def empty(request):
        return httpx.Response(200, json={"results": [], "data": []})

    journal = Journal()
    session = SelectorSession(BanditConfig(mode="fixed"), journal=journal,
                              record_dir=str(tmp_path))
    with pytest.raises(NoEvidence):
        cyclemod.run_review("seed?", 5,
                            httpx.Client(transport=httpx.MockTransport(empty)),
                            ScriptedMuse(), journal=journal,
                            record_dir=str(tmp_path), selector=session)
    obs = session.records[-1]
    assert obs["type"] == "observation" and obs["status"] == "ok"
    assert obs["reward"] < 0  # genuine zero utility, honestly negative


def test_cycle_with_bandit_freezes_scope_and_cannot_promote(tmp_path):
    out = tmp_path / "run"
    spec = RunSpec(question="seed?", max_iterations=1, max_papers=5)
    run_id = reportmod.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R")]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    bandit = BanditConfig(mode="fixed")
    res = cyclemod.run_cycle("seed?", None, None, 1, 5, muse, mock_http(),
                             journal=journal, spec=spec,
                             budget=RunBudget.from_spec(spec),
                             record_dir=str(out), bandit=bandit)
    assert res.stopped == "converged"
    state = reportmod.read_checkpoints(out)
    assert state["scope"]["bandit"]["mode"] == "fixed"
    assert (out / "bandit.jsonl").is_file()
    assert spec.maintenance is False  # goal untouched
    assert not (tmp_path / "runs").exists()  # selector cannot promote


def test_cycle_learning_budget_stop_keeps_snapshot_resumable(tmp_path):
    out = tmp_path / "run"
    spec = RunSpec(question="seed?", max_iterations=5, max_papers=5, max_model_calls=2)
    run_id = reportmod.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R1"), grounded_review("R2")]
    muse.queues["follow"] = ['[{"question": "q2?", "kind": "review", "rationale": "r"}]']
    muse.queues["final"] = ["Final."]
    bandit = BanditConfig(mode="learning", seed=4, policy_dir=str(tmp_path / "pol"))
    res = cyclemod.run_cycle("seed?", None, None, 5, 5, muse, mock_http(),
                             journal=journal, spec=spec,
                             budget=RunBudget.from_spec(spec), record_dir=str(out),
                             bandit=bandit)
    assert res.stopped == "budget_exhausted"
    applied_before = PolicyStore(tmp_path / "pol").snapshot["applied_ids"]
    assert len(applied_before) == 1  # one scored review before exhaustion
    cont = ScriptedMuse()
    cont.queues["follow"] = ["[]"]
    cont.queues["final"] = ["Final."]
    cyclemod.resume_cycle(out, cont, mock_http())
    applied_after = PolicyStore(tmp_path / "pol").snapshot["applied_ids"]
    assert len(set(applied_after)) == len(applied_after)  # exactly once
    assert set(applied_before) <= set(applied_after)


def test_bandit_scope_tamper_halts_resume(tmp_path):
    out = tmp_path / "run"
    spec = RunSpec(question="seed?", max_iterations=1, max_papers=5)
    run_id = reportmod.begin_run(out, spec, "cycle")
    journal = Journal(out / "journal.jsonl", run_id=run_id)
    muse = ScriptedMuse()
    muse.queues["review"] = [grounded_review("R")]
    muse.queues["follow"] = ["[]"]
    muse.queues["final"] = ["Final."]
    cyclemod.run_cycle("seed?", None, None, 1, 5, muse, mock_http(),
                       journal=journal, spec=spec, budget=RunBudget.from_spec(spec),
                       record_dir=str(out),
                       bandit=BanditConfig(mode="fixed"))
    path = sorted((out / "checkpoints").glob("checkpoint-*.json"))[-1]
    state = json.loads(path.read_text(encoding="utf-8"))
    other = BanditConfig(mode="learning", seed=1, policy_dir=str(tmp_path / "pol"))
    with pytest.raises(cyclemod.ResumeError, match="scope mismatch"):
        cyclemod.run_cycle("seed?", None, None, 1, 5, ScriptedMuse(), mock_http(),
                           journal=Journal(), spec=spec, record_dir=str(out),
                           resume=dict(state), bandit=other)


def test_invalid_checkpoint_bandit_block_halts(tmp_path):
    out = tmp_path / "run"
    spec = RunSpec(question="seed?")
    run_id = reportmod.begin_run(out, spec, "cycle")
    (out / "checkpoints").mkdir()
    (out / "checkpoints" / "checkpoint-0000.json").write_text(json.dumps({
        "schema_version": 2, "seq": 0, "run_id": run_id, "spec": spec.to_dict(),
        "iterations": [], "pending": [], "seen": [], "failed_q": None,
        "budget": RunBudget.from_spec(spec).to_dict(),
        "scope": {"maintenance": False, "repo_root": None, "check_cmd": None,
                  "revise_rounds": 1, "bandit": {"mode": "learning"}},
        "code_hash": reportmod.code_revision(),
        "csv_hash": reportmod.file_hash(None)}), encoding="utf-8")
    (out / "journal.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(cyclemod.ResumeError, match="bandit block"):
        cyclemod.resume_cycle(out, ScriptedMuse(), mock_http())


def test_cli_build_bandit_validation(tmp_path):
    from canary import cli as climod
    from canary.spec import InvalidSpec

    args = type("Args", (), {"bandit_mode": "off"})
    assert climod.build_bandit(args) is None
    args = type("Args", (), {"bandit_mode": "learning", "bandit_eps": 0.9,
                             "bandit_seed": 0, "bandit_dir": str(tmp_path)})
    with pytest.raises(InvalidSpec, match="bandit config"):
        climod.build_bandit(args)
    args = type("Args", (), {"bandit_mode": "learning", "bandit_eps": 0.1,
                             "bandit_seed": 0, "bandit_dir": None})
    with pytest.raises(InvalidSpec, match="policy_dir"):
        climod.build_bandit(args)


# --- controlled comparison ---


def test_fixture_table_is_mixed_by_design():
    dev, held = fixture_questions()
    assert [q.id for q in dev] == ["d1", "d2", "d3", "d4"]
    assert [q.id for q in held] == ["h1", "h2"]
    winners = set()
    for fixture in dev + held:
        for sid in STRATEGY_IDS:
            if len(fixture_papers(fixture, sid)) == 2:
                winners.add((fixture.id, sid))
    strategies_winning = {sid for _, sid in winners}
    assert strategies_winning == {"original-v1", "focused-v1", "terms-v1", "gap-v1"}


def test_empty_branch_scores_genuine_zero(tmp_path):
    journal = Journal()
    session = SelectorSession(BanditConfig(mode="fixed", fixed_strategy="focused-v1"),
                              journal=journal)
    dev, _ = fixture_questions()
    d2 = next(q for q in dev if q.id == "d2")
    row = run_fixture_question(d2, session, journal, tmp_path / "q")
    assert row["completed"] is False and row["no_evidence"] is True
    assert row["status"] == "ok" and row["reward"] < 0
    assert row["strategy"] == "focused-v1"


def test_compare_three_arm_schema_costs_and_freeze(tmp_path):
    report = compare_three_arm(tmp_path / "cmp", seed=7, epsilon=0.2)
    assert report["comparison_version"] == 1
    assert report["dev_ids"] == ["d1", "d2", "d3", "d4"]
    assert report["held_ids"] == ["h1", "h2"]
    assert len(report["disclaimers"]) == 3
    for arm in ("A", "B", "C"):
        assert len(report["dev"][arm]["rows"]) == 4
        assert len(report["held"][arm]["rows"]) == 2
    costs = report["costs"]["C"]
    assert costs["model_calls"] > 0 and costs["provider_calls"] > 0
    assert costs["policy_bytes"] > 0 and costs["memory_bytes"] > 0
    assert costs["decision_ms_total"] > 0
    # Frozen held-out: policy learned only from dev decisions.
    applied = set(PolicyStore(tmp_path / "cmp" / "policy-C").snapshot["applied_ids"])
    dev_ids = {r["decision_id"] for r in report["dev"]["C"]["rows"]}
    held_ids = {r["decision_id"] for r in report["held"]["C"]["rows"]}
    assert applied and applied <= dev_ids and not (applied & held_ids)
    # B/C memory parity: identical lesson counts, selector is the only delta.
    mem_b = json.loads((tmp_path / "cmp" / "arm-B" / "memory.jsonl").read_text())
    mem_c = json.loads((tmp_path / "cmp" / "arm-C" / "memory.jsonl").read_text())
    assert len(mem_b) == len(mem_c) == 6


def test_compare_is_deterministic(tmp_path):
    first = compare_three_arm(tmp_path / "one", seed=7, epsilon=0.2)
    second = compare_three_arm(tmp_path / "two", seed=7, epsilon=0.2)

    def scrub(node):
        if isinstance(node, dict):
            return {k: scrub(v) for k, v in node.items()
                    if k not in ("decision_ms", "wall_ms", "decision_ms_total",
                                 "held_decision_ms_total")}
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return node

    assert scrub(first) == scrub(second)
    snap1 = (tmp_path / "one" / "policy-C" / "snapshot.json").read_bytes()
    snap2 = (tmp_path / "two" / "policy-C" / "snapshot.json").read_bytes()
    assert json.loads(snap1)["arms"] == json.loads(snap2)["arms"]


def test_compare_overhead_is_measured_and_small(tmp_path):
    report = compare_three_arm(tmp_path / "cmp", seed=7, epsilon=0.2)
    latencies = [r["decision_ms"] for arm in report["dev"].values() for r in arm["rows"]]
    latencies += [r["decision_ms"] for arm in report["held"].values() for r in arm["rows"]]
    assert max(latencies) < 1000  # pathology gate only; actuals are ~ms
    assert report["costs"]["C"]["policy_bytes"] < 100_000


def test_counterfactual_ips_from_logged_data():
    from canary.strategy import counterfactual_ips

    decisions = [
        {"id": "d0", "selected": "original-v1", "p_selected": 0.5,
         "features": [0.1] * 6},
        {"id": "d1", "selected": "focused-v1", "p_selected": 0.5,
         "features": [0.9] * 6},
        {"id": "d2", "selected": "original-v1", "p_selected": 0.5,
         "features": [0.1] * 6},
    ]
    observations = [
        {"decision_id": "d0", "status": "ok", "reward": 0.4},
        {"decision_id": "d1", "status": "ok", "reward": 0.8},
        {"decision_id": "d2", "status": "unavailable", "reward": 0.0},
    ]
    always_original = counterfactual_ips(
        decisions, observations, lambda features: "original-v1")
    # Only d0 matches (d2 has no usable reward): (0.4/0.5)/3.
    assert always_original == {"ips": pytest.approx(0.8 / 3), "matched": 1, "total": 3}
    never_matching = counterfactual_ips(
        decisions, observations, lambda features: "gap-v1")
    assert never_matching == {"ips": 0.0, "matched": 0, "total": 3}


def test_selector_restart_continues_counter_weights_and_rng(tmp_path):
    def cfg(path):
        return BanditConfig(mode="learning", epsilon=0.3, seed=6, policy_dir=str(path))

    def outcome():
        return {"question": "q?", "papers": [], "claims": (),
                "validation": {}, "dedupe": {"candidates": 0, "kept": 0, "merged": 0},
                "n_provider_calls": 2, "n_model_calls": 0, "providers_ok": True}

    whole = SelectorSession(cfg(tmp_path / "whole"))
    solo = [whole.decide(f"q{i}?") for i in range(2)]
    for decision in solo:
        whole.score_and_observe(decision, outcome())
    third_solo = whole.decide("q2?")
    first = SelectorSession(cfg(tmp_path / "pol"))
    split = [first.decide(f"q{i}?") for i in range(2)]
    for decision in split:
        first.score_and_observe(decision, outcome())
    second = SelectorSession(cfg(tmp_path / "pol"))  # restart from snapshot
    resumed = second.decide("q2?")
    assert resumed.id.endswith("d000002")  # counter persisted
    assert resumed.strategy_id == third_solo.strategy_id  # weights + RNG continue
    out = second.store.observe({"id": split[0].id, "strategy_id": "original-v1",
                                "status": "ok", "features": [0.0] * 6,
                                "reward": 0.1})
    assert out["outcome"] == "duplicate"  # exactly once across restart
    assert second.store.snapshot["updates"] == 2


def test_session_level_evaluator_failure_is_status_not_raise(tmp_path):
    session = SelectorSession(BanditConfig(mode="learning", seed=2,
                                           policy_dir=str(tmp_path / "pol")))
    decision = session.decide("Does X matter?")
    obs = session.score_and_observe(decision, {
        "question": "Does X matter?", "papers": [], "claims": object(),
        "validation": {}, "dedupe": {}, "n_provider_calls": 2,
        "n_model_calls": 1, "providers_ok": True})
    assert obs["status"] == "evaluator-failed"
    assert obs["update"]["applied"] is False
    assert session.store.snapshot["updates"] == 0
    missing = session.score_and_observe(decision, None)  # no outcome at all
    assert missing["status"] == "unavailable"
