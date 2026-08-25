"""Semantic archetype classifier — TRD §1.2 (proxy features, predicted labels) and §2.

Labels each request with its archetype using ONLY pre-inference proxy features, i.e.
signals a real ingress layer could read from the request envelope before the model runs:

- ``declared_output_len``  — the caller's requested/declared max output length.
- ``has_tool_schema``      — whether the request carries a tool/function schema.
- ``prompt_domain_score``  — a cheap lexical/domain score over the prompt.

``true_archetype`` is the training LABEL only. It is never used as a feature, and
``classify`` never reads it — that separation is what makes the accuracy number
meaningful rather than circular.

Acceptance gate (TRD §6): held-out accuracy > 0.90.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier

from src.config import DATA_DIR

# The three pre-inference proxy features (TRD §1.2). This list is the feature contract:
# the classifier is fit on exactly these columns, in this order.
FEATURE_COLUMNS: list[str] = ["declared_output_len", "has_tool_schema", "prompt_domain_score"]

# Per-archetype proxy-feature profiles. Each archetype gets a distinct signature:
#   short_conversational — very short declared output, rarely any tool schema, low domain score
#   long_context_rag     — long declared output and a high retrieval-domain score
#   agentic_tool_using   — mid-length output but almost always carries a tool schema
#   batch_offline        — mid-length output, mid domain score, tool schema uncommon
# Gaussian noise on the continuous features and Bernoulli noise on the flag make the
# classes overlap, so the accuracy gate is a real test rather than a lookup.
OUTPUT_LEN_PROFILE: dict[str, tuple[float, float]] = {  # archetype -> (mean, std)
    "short_conversational": (150.0, 40.0),
    "long_context_rag": (800.0, 150.0),
    "agentic_tool_using": (450.0, 120.0),
    "batch_offline": (300.0, 90.0),
}
TOOL_SCHEMA_PROBABILITY: dict[str, float] = {
    "short_conversational": 0.02,
    "long_context_rag": 0.05,
    "agentic_tool_using": 0.95,
    "batch_offline": 0.10,
}
DOMAIN_SCORE_PROFILE: dict[str, tuple[float, float]] = {  # archetype -> (mean, std)
    "short_conversational": (0.20, 0.08),
    "long_context_rag": (0.85, 0.07),
    "agentic_tool_using": (0.45, 0.10),
    "batch_offline": (0.55, 0.10),
}

OUTPUT_LEN_FLOOR: float = 1.0
TEST_SIZE: float = 0.3
MAX_DEPTH: int = 6  # shallow enough that the gate measures signal, not memorisation


def add_proxy_features(df: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    """Derive the three pre-inference proxy features from ``true_archetype``.

    Simulates what an ingress layer would observe about a request before inference.
    Returns a new DataFrame (the input is not mutated). Seeded via
    ``np.random.default_rng(seed)`` so runs are reproducible (NFR-2).
    """
    if "true_archetype" not in df.columns:
        raise KeyError("add_proxy_features requires a 'true_archetype' column (TRD §1.2)")

    rng = np.random.default_rng(seed)
    archetypes = df["true_archetype"].to_numpy()
    n_rows = len(df)

    len_mean = np.array([OUTPUT_LEN_PROFILE[a][0] for a in archetypes], dtype=float)
    len_std = np.array([OUTPUT_LEN_PROFILE[a][1] for a in archetypes], dtype=float)
    declared_output_len = np.maximum(rng.normal(len_mean, len_std), OUTPUT_LEN_FLOOR)

    tool_probability = np.array([TOOL_SCHEMA_PROBABILITY[a] for a in archetypes], dtype=float)
    has_tool_schema = (rng.random(n_rows) < tool_probability).astype(int)

    domain_mean = np.array([DOMAIN_SCORE_PROFILE[a][0] for a in archetypes], dtype=float)
    domain_std = np.array([DOMAIN_SCORE_PROFILE[a][1] for a in archetypes], dtype=float)
    prompt_domain_score = np.clip(rng.normal(domain_mean, domain_std), 0.0, 1.0)

    out = df.copy()
    out["declared_output_len"] = declared_output_len
    out["has_tool_schema"] = has_tool_schema
    out["prompt_domain_score"] = prompt_domain_score
    return out


def train_classifier(df: pd.DataFrame, seed: int = 0) -> DecisionTreeClassifier:
    """Fit a DecisionTreeClassifier on the three proxy features and report held-out accuracy.

    ``seed`` is a keyword-compatible addition to the TRD §2 signature so the split and the
    tree are deterministic (NFR-2); ``train_classifier(df)`` still works exactly as the
    TRD specifies.

    The fitted estimator carries the held-out score as ``holdout_accuracy_`` so callers
    (tests, the recalibration loop) can assert on it without re-splitting.
    """
    missing = [c for c in FEATURE_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(f"missing proxy features {missing}; call add_proxy_features first")

    features = df[FEATURE_COLUMNS]
    labels = df["true_archetype"]

    x_train, x_test, y_train, y_test = train_test_split(
        features,
        labels,
        test_size=TEST_SIZE,
        random_state=seed,
        shuffle=True,
        stratify=labels,
    )

    clf = DecisionTreeClassifier(max_depth=MAX_DEPTH, random_state=seed)
    clf.fit(x_train, y_train)

    accuracy = float(accuracy_score(y_test, clf.predict(x_test)))
    # sklearn estimators permit extra fitted attributes; the trailing-underscore name
    # follows sklearn's own convention for "set during fit".
    clf.holdout_accuracy_ = accuracy
    print(f"classifier held-out accuracy: {accuracy:.4f} (gate: > 0.90)")
    return clf


def classify(df: pd.DataFrame, clf: DecisionTreeClassifier) -> pd.DataFrame:
    """Add ``predicted_archetype`` and ``confidence`` columns per TRD §1.2.

    ``confidence`` is the classifier's max class probability, in [0, 1]. This function
    reads only ``FEATURE_COLUMNS`` — never ``true_archetype``.
    """
    missing = [c for c in FEATURE_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(f"missing proxy features {missing}; call add_proxy_features first")

    features = df[FEATURE_COLUMNS]
    probabilities = clf.predict_proba(features)  # shape (n_samples, n_classes)

    out = df.copy()
    out["predicted_archetype"] = clf.classes_[np.argmax(probabilities, axis=1)]
    out["confidence"] = probabilities.max(axis=1)
    return out


def main() -> None:
    """Read ``data/trace.csv``, run the full pipeline, write ``data/trace_labeled.csv``."""
    trace_path = DATA_DIR / "trace.csv"
    if not trace_path.is_file():
        raise FileNotFoundError(f"{trace_path} not found; run `python -m src.trace_gen.generator` first")

    df = pd.read_csv(trace_path)
    featured = add_proxy_features(df, seed=0)
    clf = train_classifier(featured, seed=0)
    labeled = classify(featured, clf)

    out_path = DATA_DIR / "trace_labeled.csv"
    labeled.to_csv(out_path, index=False)

    full_accuracy = float(accuracy_score(labeled["true_archetype"], labeled["predicted_archetype"]))
    print(f"wrote {out_path} ({len(labeled)} labeled requests)")
    print(f"whole-trace accuracy: {full_accuracy:.4f}   mean confidence: {labeled['confidence'].mean():.4f}")
    print("\nper-archetype recall:")
    for archetype, group in labeled.groupby("true_archetype"):
        recall = float((group["predicted_archetype"] == archetype).mean())
        print(f"  {archetype:>22}: {recall:.4f}  (n={len(group)})")


if __name__ == "__main__":
    main()
