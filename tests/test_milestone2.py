import numpy as np
import pandas as pd
import pytest
from sklearn.datasets import make_classification, make_regression

from autoresearch import analysis, data as datamod, modeling, report


class FakeCompleter:
    model = "fake"

    def __init__(self, text="Findings text."):
        self.text = text
        self.seen = {}

    def complete(self, system, user, max_tokens=8000):
        self.seen = {"system": system, "user": user}
        return self.text


def write_csv(path, df):
    path.write_text(df.to_csv(index=False), encoding="utf-8")
    return str(path)


@pytest.fixture()
def clf_csv(tmp_path):
    X, y = make_classification(n_samples=200, n_features=6, n_informative=4, random_state=0)
    df = pd.DataFrame(X, columns=[f"f{i}" for i in range(6)])
    df["target"] = y
    df.loc[::10, "f0"] = np.nan  # missing values tolerated
    df["group"] = np.where(df["f1"] > 0, "a", "b")  # categorical tolerated
    return write_csv(tmp_path / "clf.csv", df)


@pytest.fixture()
def reg_csv(tmp_path):
    X, y = make_regression(n_samples=200, n_features=5, noise=5.0, random_state=1)
    df = pd.DataFrame(X, columns=[f"x{i}" for i in range(5)])
    df["price"] = y
    return write_csv(tmp_path / "reg.csv", df)


# --- data ---


def test_task_inference(clf_csv, reg_csv):
    assert datamod.profile_frame(datamod.load_csv(clf_csv), "target").task == "classification"
    assert datamod.profile_frame(datamod.load_csv(reg_csv), "price").task == "regression"


def test_missing_target_and_empty_rejected(tmp_path):
    with pytest.raises(ValueError, match="not in columns"):
        datamod.profile_frame(pd.DataFrame({"a": range(30)}), "nope")
    with pytest.raises(FileNotFoundError):
        datamod.load_csv(tmp_path / "missing.csv")
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty|parse"):
        datamod.load_csv(empty)


def test_too_few_rows_rejected(tmp_path):
    df = pd.DataFrame({"f": range(10), "t": [0, 1] * 5})
    with pytest.raises(ValueError, match="at least 20"):
        datamod.profile_frame(df, "t")


def test_prepare_handles_mixed_types(clf_csv):
    prep = datamod.prepare(datamod.load_csv(clf_csv), "target")
    assert prep.X_train.shape[1] == len(prep.feature_names) >= 7  # 6 numeric + one-hot group
    assert not np.isnan(prep.X_train).any()
    assert len(prep.y_train) + len(prep.y_test) == 200


def test_high_cardinality_dropped(tmp_path):
    df = pd.DataFrame({"id": [f"row{i}" for i in range(60)], "f": np.random.RandomState(0).rand(60)})
    df["t"] = (df["f"] > 0.5).astype(int)
    prof = datamod.profile_frame(df, "t")
    assert "id" in prof.dropped_features


# --- modeling ---


def test_classification_beats_dummy(clf_csv):
    res = modeling.run(datamod.prepare(datamod.load_csv(clf_csv), "target"))
    assert res.task == "classification" and res.best != "dummy"
    assert res.best_test > res.baseline_test + 0.1
    assert len(res.importances) > 0


def test_regression_beats_dummy(reg_csv):
    res = modeling.run(datamod.prepare(datamod.load_csv(reg_csv), "price"))
    assert res.task == "regression" and res.best != "dummy"
    assert res.best_test < res.baseline_test
    assert "R²" in res.metric


def test_modeling_is_deterministic(clf_csv):
    df = datamod.load_csv(clf_csv)
    a = modeling.run(datamod.prepare(df, "target"))
    b = modeling.run(datamod.prepare(df, "target"))
    assert a.scores == b.scores and a.importances == b.importances


def test_weak_signal_warns(tmp_path):
    rng = np.random.RandomState(3)
    df = pd.DataFrame(rng.rand(120, 4), columns=list("abcd"))
    df["t"] = rng.randint(0, 2, 120)  # pure noise target
    res = modeling.run(datamod.prepare(df, "t"))
    assert any("weak" in w or "barely" in w or "within noise" in w for w in res.warnings)


# --- analysis + report ---


def test_narrate_uses_numbers_only(clf_csv):
    df = datamod.load_csv(clf_csv)
    prep = datamod.prepare(df, "target")
    res = modeling.run(prep)
    fake = FakeCompleter()
    f = analysis.narrate("what predicts target?", prep, res, fake)
    assert f.model == "fake"
    assert "cv " in fake.seen["user"] and "Selected" in fake.seen["user"]
    assert "ONLY the numbers" in fake.seen["system"]
    with pytest.raises(RuntimeError, match="empty"):
        analysis.narrate("q", prep, res, FakeCompleter("  "))


def test_analysis_bundle(tmp_path, clf_csv):
    df = datamod.load_csv(clf_csv)
    prep = datamod.prepare(df, "target")
    res = modeling.run(prep)
    f = analysis.Findings(text="Plain findings.", model="m")
    out = report.write_analysis_bundle(tmp_path / "out", "q?", clf_csv, "target", prep, res, f)
    md = (out / "analysis.md").read_text()
    assert "Plain findings." in md and "## Evidence" in md
    assert (out / "provenance.json").exists()


def test_latin1_csv_loads(tmp_path):
    p = tmp_path / "latin.csv"
    body = "name,val,t\n" + ("caf\xe9,1,0\nz\xfcrich,2,1\n" * 15)
    p.write_bytes(body.encode("latin-1"))
    df = datamod.load_csv(p)
    assert len(df) == 30 and "caf\xe9" in df["name"].tolist()


def test_row_cap_samples_deterministically(monkeypatch):
    monkeypatch.setattr(datamod, "ROW_CAP", 50)
    rng = np.random.RandomState(0)
    df = pd.DataFrame({"f": rng.rand(80), "t": (rng.rand(80) > 0.5).astype(int)})
    a = datamod.prepare(df, "t")
    b = datamod.prepare(df, "t")
    assert len(a.y_train) + len(a.y_test) == 50
    assert "sampled 50/" in a.preprocessing
    assert list(a.y_train) == list(b.y_train)


def test_all_missing_categorical_dropped():
    df = pd.DataFrame({"gone": [None] * 30, "f": np.random.RandomState(0).rand(30)})
    df["t"] = (df["f"] > 0.5).astype(int)
    prof = datamod.profile_frame(df, "t")
    assert "gone" in prof.dropped_features
    prep = datamod.prepare(df, "t")  # must not crash the imputer
    assert not np.isnan(prep.X_train).any()


def test_singleton_class_survives_split_and_cv():
    rng = np.random.RandomState(0)
    df = pd.DataFrame({"f1": rng.rand(40), "f2": rng.rand(40)})
    df["t"] = 0
    df.loc[0, "t"] = 1  # lone member
    prep = datamod.prepare(df, "t")
    assert "pinned 1" in prep.preprocessing
    assert 1 in set(prep.y_train)
    res = modeling.run(prep)
    assert any("degenerate" in w for w in res.warnings)
