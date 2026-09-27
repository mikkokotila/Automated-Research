"""Build 11: leakage-free fitting, predictive-vs-causal honesty."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from sklearn.compose import ColumnTransformer
from sklearn.datasets import make_classification, make_regression

from canary import analysis, data as datamod, modeling, report
from canary.cycle import NoEvidence, run_analyze, split_seed


@pytest.fixture()
def clf_df():
    X, y = make_classification(n_samples=200, n_features=6, n_informative=4, random_state=0)
    df = pd.DataFrame(X, columns=[f"f{i}" for i in range(6)])
    df["target"] = y
    df["group"] = np.where(df["f1"] > 0, "a", "b")
    return df


@pytest.fixture()
def reg_df():
    X, y = make_regression(n_samples=200, n_features=5, noise=5.0, random_state=1)
    df = pd.DataFrame(X, columns=[f"x{i}" for i in range(5)])
    df["price"] = y
    return df


FIT_RECORDS: list[frozenset] = []


def _record_index(X):
    idx = X.index if hasattr(X, "index") else pd.DataFrame(X).index
    FIT_RECORDS.append(frozenset(idx))


class RecordingPreprocessor(ColumnTransformer):
    """ColumnTransformer that logs which row indices each fit sees."""

    def fit(self, X, y=None, **params):
        _record_index(X)
        return super().fit(X, y, **params)

    def fit_transform(self, X, y=None, **params):
        _record_index(X)
        return super().fit_transform(X, y, **params)


def recording_factory(numeric, categorical):
    base = datamod.build_preprocessor(numeric, categorical)
    rec = RecordingPreprocessor(base.transformers)
    rec.set_params(**base.get_params(deep=False))
    return rec


# --- leakage ---


def test_preprocessing_fits_see_only_fold_train_rows(clf_df):
    FIT_RECORDS.clear()
    prep = datamod.prepare(clf_df, "target")
    modeling.run(prep, preprocessor_factory=recording_factory)
    train_idx = frozenset(prep.train_frame.index)
    holdout_idx = frozenset(idx for idx in clf_df.dropna(subset=["target"]).index
                           if idx not in train_idx)
    assert len(FIT_RECORDS) >= 6  # folds x models, never a single whole-train fit
    for record in FIT_RECORDS:
        assert record < train_idx  # strict subset: this fold's train only
        assert not record & holdout_idx  # the holdout never leaks into any fit
    assert frozenset().union(*FIT_RECORDS) == train_idx


def test_holdout_cannot_influence_selection(clf_df):
    prep = datamod.prepare(clf_df, "target")
    res = modeling.run(prep)
    rng = np.random.RandomState(0)
    shuffled = replace(prep, y_test=rng.permutation(prep.y_test))
    res2 = modeling.run(shuffled)
    assert res2.best == res.best
    assert [s.cv_mean for s in res2.scores] == [s.cv_mean for s in res.scores]
    assert res2.best_test != res.best_test  # the holdout only moves reported numbers


def test_split_seed_rotates_holdout_but_replays(clf_df):
    assert split_seed("c", "t", "q1") == split_seed("c", "t", "q1")
    assert split_seed("c", "t", "q1") != split_seed("c", "t", "q2")
    a = datamod.prepare(clf_df, "target", seed=split_seed("c", "t", "q1"))
    b = datamod.prepare(clf_df, "target", seed=split_seed("c", "t", "q2"))
    c = datamod.prepare(clf_df, "target", seed=split_seed("c", "t", "q1"))
    assert list(a.y_test) != list(b.y_test)
    assert list(a.y_test) == list(c.y_test) and list(a.y_train) == list(c.y_train)
    assert f"seed {a.seed}" in a.preprocessing


# --- hostile data ---


def test_sparse_columns_survive(clf_df):
    clf_df["sparse"] = pd.arrays.SparseArray(np.random.RandomState(0).rand(len(clf_df)))
    res = modeling.run(datamod.prepare(clf_df, "target"))
    assert res.best != "dummy"


def test_rare_two_member_class_works():
    rng = np.random.RandomState(0)
    df = pd.DataFrame({"f1": rng.rand(40), "f2": rng.rand(40)})
    df["t"] = 0
    df.loc[0, "t"] = 1
    df.loc[1, "t"] = 1
    res = modeling.run(datamod.prepare(df, "t"))
    assert res.task == "classification"


def test_tiny_sample_warns_but_completes():
    rng = np.random.RandomState(0)
    df = pd.DataFrame({"f": rng.rand(20)})
    df["t"] = (df["f"] > 0.5).astype(int)
    res = modeling.run(datamod.prepare(df, "t"))
    assert any("small sample" in w for w in res.warnings)


def test_inf_target_fails_actionably(reg_df):
    reg_df.loc[0, "price"] = np.inf
    with pytest.raises(ValueError, match="inf|non-finite|infinity"):
        modeling.run(datamod.prepare(reg_df, "price"))


def test_inf_feature_fails_actionably(reg_df):
    reg_df.loc[0, "x0"] = np.inf
    with pytest.raises(ValueError, match="inf|non-finite|infinity"):
        modeling.run(datamod.prepare(reg_df, "price"))


def test_feature_explosion_refused(monkeypatch, clf_df):
    monkeypatch.setattr(datamod, "MAX_FEATURES", 5)
    with pytest.raises(ValueError, match="explosion"):
        datamod.prepare(clf_df, "target")


def test_task_override_accepted_and_validated(clf_df):
    prep = datamod.prepare(clf_df, "target", task="regression")
    assert prep.profile.task == "regression"
    with pytest.raises(ValueError, match="task override"):
        datamod.prepare(clf_df, "target", task="causal")


def test_grouped_splits_rejected_explicitly(clf_df):
    with pytest.raises(ValueError, match="not supported.*i.i.d"):
        datamod.prepare(clf_df, "target", group_col="group")


# --- predictive vs causal ---


@pytest.mark.parametrize("question", [
    "what causes readmission?",
    "what is the effect of dosage on recovery?",
    "does tutoring affect test scores?",
    "estimate the causal impact of delay on mortality",
    "is the association confounded by age?",
])
def test_causal_questions_detected(question):
    assert analysis.classify_analysis(question) == "causal"


@pytest.mark.parametrize("question", [
    "what predicts readmission?",
    "does delay raise mortality?",
    "which factors relate to churn?",
    "what predicts t?",
])
def test_association_questions_pass(question):
    assert analysis.classify_analysis(question) == "predictive"


def test_causal_question_refused_with_guidance(tmp_path, clf_df):
    csv = tmp_path / "d.csv"
    csv.write_text(clf_df.to_csv(index=False), encoding="utf-8")

    class Muse:
        model = "m"

        def complete(self, system, user, max_tokens=8000):
            raise AssertionError("no model call on refusal")

    with pytest.raises(NoEvidence, match="quasi-experimental"):
        run_analyze("what causes churn?", str(csv), "target", Muse())


def test_importance_uses_selection_metric(clf_df, reg_df, monkeypatch):
    seen = {}
    import sklearn.inspection as _insp

    real = _insp.permutation_importance

    def recorder(estimator, X, y, **kwargs):
        seen["scoring"] = kwargs.get("scoring")
        return real(estimator, X, y, **kwargs)

    monkeypatch.setattr(modeling, "permutation_importance", recorder)
    modeling.run(datamod.prepare(clf_df, "target"))
    assert seen["scoring"] == "accuracy"
    modeling.run(datamod.prepare(reg_df, "price"))
    assert seen["scoring"] == "neg_root_mean_squared_error"


def test_kind_labeled_through_prompt_and_bundle(tmp_path, clf_df):
    prep = datamod.prepare(clf_df, "target")
    res = modeling.run(prep)
    assert res.kind == "predictive-association"
    prompt = analysis.build_prompt("what predicts target?", prep, res)
    assert "predictive-association" in prompt and "not causation" in prompt

    class Muse:
        model = "m"

        def complete(self, system, user, max_tokens=8000):
            return "Findings."

    f = analysis.narrate("what predicts target?", prep, res, Muse())
    out = report.write_analysis_bundle(tmp_path, "q?", "d.csv", "target", prep, res, f)
    md = (out / "analysis.md").read_text()
    assert "predictive-association" in md and "not causation" in md
    import json

    prov = json.loads((out / "provenance.json").read_text())
    assert prov["analysis_kind"] == "predictive-association"
    assert prov["split_seed"] == prep.seed
