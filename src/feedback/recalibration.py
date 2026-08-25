"""Feedback / recalibration loop — TRD §2.

Re-fits the archetype classifier on an expanding window of post-inference telemetry, so
labelling quality tracks the workload instead of decaying as composition drifts.

Training logic is NOT duplicated here: ``train_classifier`` is imported from
``src.classifier`` per the T6.1 requirement and Anti-Hallucination rule 3. The caller
owns before/after accuracy logging.
"""

from __future__ import annotations

import pandas as pd
from sklearn.tree import DecisionTreeClassifier

from src.classifier.archetype_classifier import FEATURE_COLUMNS, train_classifier

# Below this many observations a re-fit is noise rather than learning.
MIN_ROWS_FOR_REFIT: int = 50


def recalibrate(all_labeled_so_far_df: pd.DataFrame, seed: int = 0) -> DecisionTreeClassifier:
    """Re-fit the classifier on everything observed so far and return the new estimator.

    ``seed`` is a keyword-compatible addition to the TRD §2 signature so the re-fit is
    deterministic (NFR-2); ``recalibrate(df)`` still matches the TRD exactly.

    The returned estimator carries ``holdout_accuracy_`` from ``train_classifier``.
    """
    if len(all_labeled_so_far_df) < MIN_ROWS_FOR_REFIT:
        raise ValueError(
            f"need at least {MIN_ROWS_FOR_REFIT} rows to recalibrate, "
            f"got {len(all_labeled_so_far_df)}"
        )

    missing = [c for c in FEATURE_COLUMNS if c not in all_labeled_so_far_df.columns]
    if missing:
        raise KeyError(f"missing proxy features {missing}; call add_proxy_features first")

    return train_classifier(all_labeled_so_far_df, seed=seed)
