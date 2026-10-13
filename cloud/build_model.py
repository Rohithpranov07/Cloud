"""Train the router's classifier at image-build time (Addendum §11.2, NFR-A5).

Uses the same functions and the same seeds as ``run_comparison.simulate()`` — the
trace config's seed (42), not the seed-0 defaults of ``src.classifier.main`` — so the
live router's predictions equal the simulation's pre-recalibration predictions.
"""

from __future__ import annotations

from pathlib import Path

import joblib
from sklearn.tree import DecisionTreeClassifier

from src.classifier.archetype_classifier import add_proxy_features, train_classifier
from src.config import REPO_ROOT
from src.trace_gen.generator import _default_config, generate_trace

MODEL_PATH: Path = REPO_ROOT / "model" / "classifier.joblib"


def build_model(output_path: Path = MODEL_PATH) -> DecisionTreeClassifier:
    """Train on the seed-42 trace and persist the fitted classifier with joblib."""
    cfg = _default_config()
    featured = add_proxy_features(generate_trace(cfg), seed=cfg.seed)
    clf = train_classifier(featured, seed=cfg.seed)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, output_path)
    return clf


def main() -> None:
    clf = build_model()
    print(f"wrote {MODEL_PATH} (held-out accuracy {clf.holdout_accuracy_:.4f})")


if __name__ == "__main__":
    main()
