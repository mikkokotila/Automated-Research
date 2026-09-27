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
MAX_FEATURES = 5000  # one-hot explosion guard: fail loudly instead of OOM
ROW_CAP = 100_000
RANDOM_STATE = 42
GROUPED_SPLITS_MSG = ("grouped/time-aware splits are not supported: rows are assumed i.i.d. "
                      "Pass i.i.d. rows, or aggregate to independent units before analysis.")


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
    train_frame: pd.DataFrame | None = None  # raw train rows: CV fits per-fold pipelines here
    train_target: pd.Series | None = None
    seed: int = RANDOM_STATE


def load_csv(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {p}")
    try:
        df = pd.read_csv(p)
    except UnicodeDecodeError:
        df = pd.read_csv(p, encoding="latin-1")  # fallback for legacy exports
    except Exception as e:
        raise ValueError(f"could not parse CSV {p}: {e}") from e
    if df.empty or len(df.columns) == 0:
        raise ValueError(f"CSV {p} is empty")
    return df


def infer_task(y: pd.Series) -> str:
    if pd.api.types.is_numeric_dtype(y) and y.nunique(dropna=True) > 15:
        return "regression"
    return "classification"


def profile_frame(df: pd.DataFrame, target: str, task: str | None = None) -> Profile:
    if target not in df.columns:
        raise ValueError(f"target '{target}' not in columns: {list(df.columns)[:10]}")
    if len(df) < MIN_ROWS:
        raise ValueError(f"need at least {MIN_ROWS} rows, got {len(df)}")
    if task is not None and task not in ("classification", "regression"):
        raise ValueError(f"task override must be classification|regression, got {task!r}")
    y = df[target]
    dropped_target_rows = int(y.isna().sum())
    y = y.dropna()
    if len(y) < MIN_ROWS:
        raise ValueError(f"target '{target}' has too few non-missing values ({len(y)})")
    task = task or infer_task(y)
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
            if col.isna().all():
                dropped.append(c)
                notes.append(f"dropped all-missing column '{c}'")
            elif col.nunique(dropna=True) > MAX_CAT_CARDINALITY:
                dropped.append(c)
                notes.append(f"dropped high-cardinality column '{c}' (likely an id)")
            else:
                categorical.append(c)
    if not numeric and not categorical:
        raise ValueError("no usable feature columns (all constant, ids, or missing)")
    missing = 0
    for c in feats:  # sparse-backed columns: count missing on the dense view
        col = df[c]
        if isinstance(col.dtype, pd.SparseDtype):
            col = col.sparse.to_dense()
        missing += int(col.isna().sum())
    return Profile(
        n_rows=len(df),
        n_features=len(numeric) + len(categorical),
        task=task,
        target_values=target_values,
        numeric_features=tuple(numeric),
        categorical_features=tuple(categorical),
        dropped_features=tuple(dropped),
        missing_cells=missing,
        dropped_target_rows=dropped_target_rows,
        notes=tuple(notes),
    )


def _split(X: pd.DataFrame, y: pd.Series, task: str, test_size: float, seed: int) -> tuple:
    """Stratified split that survives singleton classes (pinned to train)."""
    notes: list[str] = []
    if task != "classification":
        idx_tr, idx_te = train_test_split(X.index.to_numpy(), test_size=test_size, random_state=seed)
        return idx_tr, idx_te, notes
    counts = y.value_counts()
    tiny = counts[counts < 2].index.tolist()
    if not tiny:
        idx_tr, idx_te = train_test_split(
            X.index.to_numpy(), test_size=test_size, random_state=seed, stratify=y.to_numpy()
        )
        return idx_tr, idx_te, notes
    pinned = y[y.isin(tiny)].index.to_numpy()
    rest = y[~y.isin(tiny)]
    sub_size = test_size * len(y) / max(len(rest), 1)
    sub_size = min(max(sub_size, 0.05), 0.9)
    r_tr, r_te = train_test_split(
        rest.index.to_numpy(), test_size=sub_size, random_state=seed, stratify=rest.to_numpy()
    )
    notes.append(f"pinned {len(pinned)} singleton-class rows to train")
    return np.concatenate([pinned, r_tr]), r_te, notes


def build_preprocessor(numeric: tuple[str, ...] | list[str],
                       categorical: tuple[str, ...] | list[str]) -> ColumnTransformer:
    """Unfitted preprocessing; CV clones one per fold so folds never leak."""
    steps: list[tuple[str, Pipeline, list[str]]] = []
    if numeric:
        steps.append((
            "num",
            Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]),
            list(numeric),
        ))
    if categorical:
        steps.append((
            "cat",
            Pipeline([
                ("impute", SimpleImputer(strategy="most_frequent")),
                ("encode", OneHotEncoder(handle_unknown="ignore")),
            ]),
            list(categorical),
        ))
    return ColumnTransformer(steps)


def prepare(df: pd.DataFrame, target: str, test_size: float = 0.25,
            task: str | None = None, seed: int = RANDOM_STATE,
            group_col: str | None = None) -> Prepared:
    if group_col is not None:
        raise ValueError(GROUPED_SPLITS_MSG)
    prof = profile_frame(df, target, task)
    work = df.dropna(subset=[target])
    extra: list[str] = []
    if len(work) > ROW_CAP:  # deterministic cap: bound runtime on huge exports
        work = work.sample(n=ROW_CAP, random_state=seed).sort_index()
        extra.append(f"sampled {ROW_CAP}/{prof.n_rows} rows")
    feats = [*prof.numeric_features, *prof.categorical_features]
    X = work[feats].copy()
    for c in feats:  # sparse-backed columns densify: imputers need plain dtypes
        if isinstance(X[c].dtype, pd.SparseDtype):
            X[c] = X[c].sparse.to_dense()
    y = work[target]
    idx_tr, idx_te, split_notes = _split(X, y, prof.task, test_size, seed)
    extra.extend(split_notes)
    X_tr, X_te = X.loc[idx_tr], X.loc[idx_te]
    y_tr, y_te = y.loc[idx_tr].to_numpy(), y.loc[idx_te].to_numpy()
    pre = build_preprocessor(prof.numeric_features, prof.categorical_features)
    Xt_tr = pre.fit_transform(X_tr)  # fit on train only; the holdout stays locked
    Xt_te = pre.transform(X_te)
    if np.shape(Xt_tr)[1] > MAX_FEATURES:
        raise ValueError(f"one-hot explosion: {np.shape(Xt_tr)[1]} features exceeds "
                         f"{MAX_FEATURES}; drop high-cardinality columns")
    names: list[str] = []
    if prof.numeric_features:
        names.extend(prof.numeric_features)
    if prof.categorical_features:
        enc = pre.named_transformers_["cat"].named_steps["encode"]
        names.extend(enc.get_feature_names_out(list(prof.categorical_features)).tolist())
    desc = (
        f"numeric(median+standardize): {len(prof.numeric_features)}; "
        f"categorical(mode+onehot): {len(prof.categorical_features)}; "
        f"task {prof.task}; split {len(y_tr)}/{len(y_te)} seed {seed}"
    )
    if extra:
        desc += "; " + "; ".join(extra)
    return Prepared(
        X_train=np.asarray(Xt_tr, dtype=float),
        X_test=np.asarray(Xt_te, dtype=float),
        y_train=y_tr,
        y_test=y_te,
        feature_names=tuple(names),
        profile=prof,
        preprocessing=desc,
        train_frame=X_tr,
        train_target=y.loc[idx_tr],
        seed=seed,
    )
