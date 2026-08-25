"""Tests for the archetype classifier — TRD §1.2, §2 and the §6 ">0.90 accuracy" gate."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import accuracy_score
from sklearn.tree import DecisionTreeClassifier

from src.classifier.archetype_classifier import (
    FEATURE_COLUMNS,
    add_proxy_features,
    classify,
    train_classifier,
)
from src.config import ARCHETYPES
from src.trace_gen.generator import _default_config, generate_trace

ACCURACY_GATE = 0.90


@pytest.fixture(scope="module")
def featured() -> pd.DataFrame:
    return add_proxy_features(generate_trace(_default_config()), seed=0)


@pytest.fixture(scope="module")
def fitted(featured: pd.DataFrame) -> DecisionTreeClassifier:
    return train_classifier(featured, seed=0)


def test_proxy_features_added_with_trd_names_and_types(featured: pd.DataFrame) -> None:
    for column in FEATURE_COLUMNS:
        assert column in featured.columns
    assert pd.api.types.is_float_dtype(featured["declared_output_len"])
    assert pd.api.types.is_integer_dtype(featured["has_tool_schema"])
    assert pd.api.types.is_float_dtype(featured["prompt_domain_score"])


def test_proxy_feature_ranges(featured: pd.DataFrame) -> None:
    assert (featured["declared_output_len"] > 0).all()
    assert set(featured["has_tool_schema"].unique()) <= {0, 1}
    assert featured["prompt_domain_score"].between(0.0, 1.0).all()


def test_proxy_features_do_not_mutate_input() -> None:
    original = generate_trace(_default_config())
    columns_before = list(original.columns)
    add_proxy_features(original, seed=0)
    assert list(original.columns) == columns_before


def test_proxy_features_carry_archetype_signal(featured: pd.DataFrame) -> None:
    """Each archetype's profile shows up in the aggregate, as designed."""
    by_archetype = featured.groupby("true_archetype")
    output_len = by_archetype["declared_output_len"].mean()
    assert output_len["short_conversational"] < output_len["batch_offline"]
    assert output_len["batch_offline"] < output_len["agentic_tool_using"]
    assert output_len["agentic_tool_using"] < output_len["long_context_rag"]

    tool_rate = by_archetype["has_tool_schema"].mean()
    assert tool_rate["agentic_tool_using"] > 0.85
    assert tool_rate.drop("agentic_tool_using").max() < 0.25

    domain = by_archetype["prompt_domain_score"].mean()
    assert domain["long_context_rag"] > domain["batch_offline"] > domain["short_conversational"]


def test_proxy_features_deterministic() -> None:
    """NFR-2: same seed -> identical features."""
    trace = generate_trace(_default_config())
    pd.testing.assert_frame_equal(add_proxy_features(trace, seed=11), add_proxy_features(trace, seed=11))
    other = add_proxy_features(trace, seed=12)
    assert not other["declared_output_len"].equals(add_proxy_features(trace, seed=11)["declared_output_len"])


def test_proxy_features_require_true_archetype() -> None:
    """NFR-3: a missing label column raises rather than producing junk features."""
    with pytest.raises(KeyError, match="true_archetype"):
        add_proxy_features(pd.DataFrame({"minute": [0, 1]}), seed=0)


def test_holdout_accuracy_meets_gate(fitted: DecisionTreeClassifier) -> None:
    """TRD §6 acceptance gate: held-out accuracy > 0.90 on a fixed seed."""
    accuracy = fitted.holdout_accuracy_
    assert accuracy > ACCURACY_GATE, f"held-out accuracy {accuracy:.4f} fell below the {ACCURACY_GATE} gate"


@pytest.mark.parametrize("seed", [0, 1, 2, 7, 13])
def test_accuracy_gate_holds_across_seeds(featured: pd.DataFrame, seed: int) -> None:
    """The gate is not a fluke of one seed."""
    clf = train_classifier(featured, seed=seed)
    assert clf.holdout_accuracy_ > ACCURACY_GATE


def test_classifier_is_fit_on_exactly_the_three_proxy_features(fitted: DecisionTreeClassifier) -> None:
    assert fitted.n_features_in_ == len(FEATURE_COLUMNS)
    assert list(fitted.feature_names_in_) == FEATURE_COLUMNS
    assert set(fitted.classes_) == set(ARCHETYPES)


def test_classify_adds_trd_columns(featured: pd.DataFrame, fitted: DecisionTreeClassifier) -> None:
    labeled = classify(featured, fitted)
    assert "predicted_archetype" in labeled.columns
    assert "confidence" in labeled.columns
    assert set(labeled["predicted_archetype"].unique()) <= set(ARCHETYPES)
    assert labeled["confidence"].between(0.0, 1.0).all()
    assert len(labeled) == len(featured)


def test_classify_never_reads_true_archetype(featured: pd.DataFrame, fitted: DecisionTreeClassifier) -> None:
    """Predictions must be identical when the label column is scrambled or absent."""
    baseline = classify(featured, fitted)["predicted_archetype"]

    scrambled = featured.copy()
    rng = np.random.default_rng(0)
    scrambled["true_archetype"] = rng.permutation(scrambled["true_archetype"].to_numpy())
    assert classify(scrambled, fitted)["predicted_archetype"].equals(baseline)

    without_label = classify(featured.drop(columns=["true_archetype"]), fitted)["predicted_archetype"]
    assert without_label.equals(baseline)


def test_confidence_is_the_max_class_probability(featured: pd.DataFrame, fitted: DecisionTreeClassifier) -> None:
    sample = featured.head(200)
    labeled = classify(sample, fitted)
    probabilities = fitted.predict_proba(sample[FEATURE_COLUMNS])
    np.testing.assert_allclose(labeled["confidence"].to_numpy(), probabilities.max(axis=1))
    assert (labeled["confidence"] >= 1.0 / len(ARCHETYPES) - 1e-9).all()


def test_whole_trace_accuracy_meets_gate(featured: pd.DataFrame, fitted: DecisionTreeClassifier) -> None:
    labeled = classify(featured, fitted)
    accuracy = accuracy_score(labeled["true_archetype"], labeled["predicted_archetype"])
    assert accuracy > ACCURACY_GATE


def test_predicted_composition_tracks_the_true_shift(featured: pd.DataFrame, fitted: DecisionTreeClassifier) -> None:
    """The classifier must preserve the shift the forecaster will later need to see."""
    cfg = _default_config()
    labeled = classify(featured, fitted)
    shift_end = cfg.shift_start_min + cfg.shift_duration_min
    before = labeled[labeled["minute"] < cfg.shift_start_min]["predicted_archetype"].value_counts(normalize=True)
    after = labeled[labeled["minute"] >= shift_end]["predicted_archetype"].value_counts(normalize=True)
    assert after["agentic_tool_using"] > before["agentic_tool_using"] + 0.2


def test_missing_features_raise() -> None:
    """NFR-3: train/classify fail loudly if add_proxy_features was skipped."""
    bare = generate_trace(_default_config())
    with pytest.raises(KeyError, match="add_proxy_features"):
        train_classifier(bare, seed=0)
