"""Data: load, profile, and prepare a user CSV for hypothesis testing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

MIN_ROWS = 20
MAX_CAT_CARDINALITY = 50
RANDOM_STATE = 42


@dataclass(frozen=True)
class Profile:
    n_rows: int
    n_features: int
    task: str  # "classification" | "regression"
    target_values: str  # short summary
    numeric_features: tuple[str, ...]
    categorical_features: tuple[str, ...]
    dropped_features: tuple[str, ...]
    missing_cells: int
    dropped_target_rows: int
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Prepared:
    X_train: np.ndarray
    X_test: np.ndarray
    y_train: np.ndarray
    y_test: np.ndarray
    feature_names: tuple[str, ...]
    profile: Profile
    preprocessing: str = ""


def load_csv(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {p}")
    try:
        df = pd.read_csv(p)
    except Exception as e:
        raise ValueError(f"could not parse CSV {p}: {e}") from e
    if df.empty or len(df.columns) == 0:
        raise ValueError(f"CSV {p} is empty")
    return df


def infer_task(y: pd.Series) -> str:
    if pd.api.types.is_numeric_dtype(y) and y.nunique(dropna=True) > 15:
        return "regression"
    return "classification"


def profile_frame(df: pd.DataFrame, target: str) -> Profile:
    if target not in df.columns:
        raise ValueError(f"target '{target}' not in columns: {list(df.columns)[:10]}")
    if len(df) < MIN_ROWS:
        raise ValueError(f"need at least {MIN_ROWS} rows, got {len(df)}")
    y = df[target]
    dropped_target_rows = int(y.isna().sum())
    y = y.dropna()
    if len(y) < MIN_ROWS:
        raise ValueError(f"target '{target}' has too few non-missing values ({len(y)})")
    task = infer_task(y)
    if task == "classification":
        counts = y.value_counts()
        if len(counts) < 2:
            raise ValueError(f"target '{target}' has only one class")
        target_values = ", ".join(f"{k} (n={v})" for k, v in counts.head(8).items())
    else:
        target_values = f"mean={y.mean():.3g}, std={y.std():.3g}, range=[{y.min():.3g}, {y.max():.3g}]"
    feats = [c for c in df.columns if c != target]
    numeric, categorical, dropped = [], [], []
    notes: list[str] = []
    for c in feats:
        col = df[c]
        if pd.api.types.is_numeric_dtype(col):
            if col.nunique(dropna=True) <= 1:
                dropped.append(c)
                notes.append(f"dropped constant column '{c}'")
            else:
                numeric.append(c)
        else:
            if col.nunique(dropna=True) > MAX_CAT_CARDINALITY:
                dropped.append(c)
                notes.append(f"dropped high-cardinality column '{c}' (likely an id)")
            else:
                categorical.append(c)
    if not numeric and not categorical:
        raise ValueError("no usable feature columns (all constant, ids, or missing)")
    return Profile(
        n_rows=len(df),
        n_features=len(numeric) + len(categorical),
        task=task,
        target_values=target_values,
        numeric_features=tuple(numeric),
        categorical_features=tuple(categorical),
        dropped_features=tuple(dropped),
        missing_cells=int(df[feats].isna().sum().sum()),
        dropped_target_rows=dropped_target_rows,
        notes=tuple(notes),
    )


def prepare(df: pd.DataFrame, target: str, test_size: float = 0.25) -> Prepared:
    prof = profile_frame(df, target)
    work = df.dropna(subset=[target])
    X = work[[*prof.numeric_features, *prof.categorical_features]]
    y = work[target].to_numpy()
    stratify = y if prof.task == "classification" else None
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=test_size, random_state=RANDOM_STATE, stratify=stratify
    )
    steps: list[tuple[str, Pipeline, list[str]]] = []
    if prof.numeric_features:
        steps.append((
            "num",
            Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]),
            list(prof.numeric_features),
        ))
    if prof.categorical_features:
        steps.append((
            "cat",
            Pipeline([
                ("impute", SimpleImputer(strategy="most_frequent")),
                ("encode", OneHotEncoder(handle_unknown="ignore")),
            ]),
            list(prof.categorical_features),
        ))
    pre = ColumnTransformer(steps)
    Xt_tr = pre.fit_transform(X_tr)
    Xt_te = pre.transform(X_te)
    names: list[str] = []
    if prof.numeric_features:
        names.extend(prof.numeric_features)
    if prof.categorical_features:
        enc = pre.named_transformers_["cat"].named_steps["encode"]
        names.extend(enc.get_feature_names_out(list(prof.categorical_features)).tolist())
    desc = (
        f"numeric(median+standardize): {len(prof.numeric_features)}; "
        f"categorical(mode+onehot): {len(prof.categorical_features)}; "
        f"split 75/25 seed {RANDOM_STATE}"
    )
    return Prepared(
        X_train=np.asarray(Xt_tr, dtype=float),
        X_test=np.asarray(Xt_te, dtype=float),
        y_train=y_tr,
        y_test=y_te,
        feature_names=tuple(names),
        profile=prof,
        preprocessing=desc,
    )
