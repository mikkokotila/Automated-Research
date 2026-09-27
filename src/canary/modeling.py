"""Modeling: small model zoo, cross-validation, honest test evaluation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import accuracy_score, f1_score, mean_squared_error, r2_score
from sklearn.model_selection import KFold, cross_val_score

from .data import RANDOM_STATE, Prepared

N_IMPORTANCE = 8


@dataclass(frozen=True)
class ModelScore:
    name: str
    cv_mean: float
    cv_std: float
    test: float


@dataclass(frozen=True)
class Results:
    task: str
    metric: str
    scores: tuple[ModelScore, ...]
    best: str
    best_test: float
    baseline_test: float
    importances: tuple[tuple[str, float], ...]
    warnings: tuple[str, ...]
    kind: str = "predictive-association"  # never causal inference, never a hypothesis test


def zoo(task: str) -> list[tuple[str, object]]:
    if task == "classification":
        return [
            ("dummy", DummyClassifier(strategy="stratified", random_state=RANDOM_STATE)),
            ("logreg", LogisticRegression(max_iter=2000)),
            ("gradboost", HistGradientBoostingClassifier(random_state=RANDOM_STATE)),
        ]
    return [
        ("dummy", DummyRegressor(strategy="mean")),
        ("ridge", Ridge()),
        ("gradboost", HistGradientBoostingRegressor(random_state=RANDOM_STATE)),
    ]


def test_metric(task: str, y_true: np.ndarray, pred: np.ndarray) -> tuple[str, float]:
    if task == "classification":
        return "accuracy", float(accuracy_score(y_true, pred))
    rmse = float(np.sqrt(mean_squared_error(y_true, pred)))
    return "rmse", rmse


def run(prep: Prepared, preprocessor_factory=None) -> Results:
    """Select by leakage-free CV, evaluate once on the locked holdout.

    Every CV fold fits its own preprocessing pipeline on raw fold-train rows;
    the holdout is transformed by the train-fit preprocessor and scored exactly
    once, after selection. It never influences the choice.
    """
    from sklearn.pipeline import Pipeline as _Pipeline

    from .data import build_preprocessor as _default_factory

    task = prep.profile.task
    scoring = "accuracy" if task == "classification" else "neg_root_mean_squared_error"
    n = len(prep.y_train)
    cv: int | KFold = min(5, max(2, n // 10))
    warnings: list[str] = []
    if task == "classification":
        min_count = int(pd.Series(prep.y_train).value_counts().min())
        if min_count < 2:  # degenerate split: plain folds, flagged
            cv = KFold(n_splits=2, shuffle=True, random_state=RANDOM_STATE)
            warnings.append("a class has <2 train rows — CV folds are degenerate")
        else:
            cv = min(cv, min_count)
    better = max if task == "classification" else min
    factory = preprocessor_factory or _default_factory
    raw_available = prep.train_frame is not None and prep.train_target is not None
    scores: list[ModelScore] = []
    fitted: dict[str, object] = {}
    failed_all: set[str] = set()
    bad_test: set[str] = set()
    for name, model in zoo(task):
        if raw_available:
            pipe = _Pipeline([("pre", factory(prep.profile.numeric_features,
                                              prep.profile.categorical_features)),
                              ("est", model)])
            cv_scores = cross_val_score(pipe, prep.train_frame, prep.train_target,
                                        cv=cv, scoring=scoring)
        else:  # legacy Prepared without raw frames: CV on the provided arrays
            cv_scores = cross_val_score(model, prep.X_train, prep.y_train, cv=cv, scoring=scoring)
        if task == "regression":
            cv_scores = -cv_scores
        mean, std = float(np.nanmean(cv_scores)), float(np.nanstd(cv_scores))
        if np.isnan(mean):  # every fold failed: worst possible, flagged below
            mean = 0.0 if task == "classification" else float("inf")
            std = 0.0
            failed_all.add(name)
            warnings.append(f"{name}: all CV folds failed")
        model.fit(prep.X_train, prep.y_train)
        _, test = test_metric(task, prep.y_test, model.predict(prep.X_test))
        if not np.isfinite(test):
            warnings.append(f"{name}: non-finite test score — predictions are unusable")
            bad_test.add(name)
            test = 0.0 if task == "classification" else float("inf")
        fitted[name] = model
        scores.append(ModelScore(name, mean, std, test))
    non_dummy = [s for s in scores if s.name != "dummy"]
    best = better(non_dummy, key=lambda s: s.cv_mean)  # cv decides, test only reported
    if best.name in failed_all:
        raise ValueError(f"{best.name} failed every CV fold; no usable model — "
                         "check target quality and feature coverage")
    if best.name in bad_test:
        raise ValueError(f"{best.name} produced non-finite holdout predictions; "
                         "the result is unusable — check for inf/NaN in features or target")
    baseline = next(s for s in scores if s.name == "dummy")
    metric = "accuracy" if task == "classification" else "rmse"
    extra = ""
    if task == "classification":
        f1 = float(f1_score(prep.y_test, fitted[best.name].predict(prep.X_test), average="macro", zero_division=0))  # type: ignore[union-attr]
        extra = f" (macro-F1 {f1:.3f})"
    else:
        r2 = float(r2_score(prep.y_test, fitted[best.name].predict(prep.X_test)))  # type: ignore[union-attr]
        extra = f" (R² {r2:.3f})"
    gap = abs(best.test - best.cv_mean)
    tol = 0.1 if task == "classification" else max(0.1 * abs(best.cv_mean), 1e-9)
    if gap > tol:
        warnings.append(f"train/cv vs test gap is large ({best.cv_mean:.3f} vs {best.test:.3f}) — treat as unstable")
    if task == "classification" and best.test - baseline.test < 0.05:
        warnings.append("best model barely beats the dummy baseline — signal is weak")
    if task == "classification" and best.cv_mean - baseline.cv_mean < max(baseline.cv_std, 0.02):
        warnings.append("lead over baseline is within noise — result may be chance")
    if task == "regression" and baseline.test > 0 and (baseline.test - best.test) / baseline.test < 0.05:
        warnings.append("best model barely beats the mean baseline — signal is weak")
    if prep.profile.n_rows < 100:
        warnings.append(f"small sample (n={prep.profile.n_rows}) — wide uncertainty")
    imp = permutation_importance(
        fitted[best.name], prep.X_test, prep.y_test, n_repeats=5, random_state=RANDOM_STATE,
        scoring=scoring,  # same metric as selection and evaluation
    )
    order = np.argsort(imp.importances_mean)[::-1][:N_IMPORTANCE]
    importances = tuple((prep.feature_names[i], round(float(imp.importances_mean[i]), 4)) for i in order)
    return Results(
        task=task,
        metric=metric + extra,
        scores=tuple(scores),
        best=best.name,
        best_test=round(best.test, 4),
        baseline_test=round(baseline.test, 4),
        importances=importances,
        warnings=tuple(warnings),
    )
