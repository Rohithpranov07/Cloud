"""Tests for the recalibration loop — TRD §2 and the §6 "Feedback Loop" gate."""

from __future__ import annotations

import pandas as pd
import pytest
from sklearn.metrics import accuracy_score
from sklearn.tree import DecisionTreeClassifier

from src.classifier.archetype_classifier import add_proxy_features, classify
from src.feedback.recalibration import MIN_ROWS_FOR_REFIT, recalibrate
from src.trace_gen.generator import TraceConfig, _default_config, generate_trace


@pytest.fixture(scope="module")
def featured() -> pd.DataFrame:
    return add_proxy_features(generate_trace(_default_config()), seed=0)


def test_recalibrate_returns_a_fitted_classifier(featured: pd.DataFrame) -> None:
    clf = recalibrate(featured, seed=0)
    assert isinstance(clf, DecisionTreeClassifier)
    assert hasattr(clf, "holdout_accuracy_")
    assert 0.0 <= clf.holdout_accuracy_ <= 1.0


def test_recalibrate_reuses_train_classifier(monkeypatch: pytest.MonkeyPatch, featured: pd.DataFrame) -> None:
    """T6.1: training logic must be imported, not duplicated."""
    calls: list[int] = []
    import src.feedback.recalibration as recalibration_module

    original = recalibration_module.train_classifier

    def spy(df: pd.DataFrame, seed: int = 0) -> DecisionTreeClassifier:
        calls.append(len(df))
        return original(df, seed=seed)

    monkeypatch.setattr(recalibration_module, "train_classifier", spy)
    recalibrate(featured, seed=0)
    assert calls == [len(featured)]


def test_recalibrate_is_deterministic(featured: pd.DataFrame) -> None:
    """NFR-2: same window, same seed -> same accuracy and same predictions."""
    first = recalibrate(featured, seed=0)
    second = recalibrate(featured, seed=0)
    assert first.holdout_accuracy_ == second.holdout_accuracy_
    assert classify(featured, first)["predicted_archetype"].equals(
        classify(featured, second)["predicted_archetype"]
    )


def test_recalibrate_rejects_too_little_data(featured: pd.DataFrame) -> None:
    """NFR-3: a re-fit on a handful of rows is noise, so it raises rather than pretending."""
    with pytest.raises(ValueError, match="at least"):
        recalibrate(featured.head(MIN_ROWS_FOR_REFIT - 1), seed=0)


def test_recalibrate_requires_proxy_features() -> None:
    with pytest.raises(KeyError, match="add_proxy_features"):
        recalibrate(generate_trace(_default_config()), seed=0)


def test_expanding_window_holds_or_improves_accuracy(featured: pd.DataFrame) -> None:
    """TRD §6 gate: accuracy after recalibrating on an expanded window is >= before.

    Measured on the whole trace, which is the fair comparison: a classifier fit on the
    first 60 minutes has never seen the post-shift composition, while one fit on 90
    minutes has. Both are scored on identical data.
    """
    windows = [60, 90]
    accuracies: list[float] = []
    for window_end in windows:
        window = featured[featured["minute"] < window_end]
        clf = recalibrate(window, seed=0)
        labeled = classify(featured, clf)
        accuracies.append(float(accuracy_score(labeled["true_archetype"], labeled["predicted_archetype"])))
        print(f"window 0-{window_end} min ({len(window)} rows): whole-trace accuracy {accuracies[-1]:.4f}")

    assert accuracies[1] >= accuracies[0] - 1e-9, (
        f"expanding the window from {windows[0]} to {windows[1]} minutes hurt accuracy: "
        f"{accuracies[0]:.4f} -> {accuracies[1]:.4f}"
    )


def test_gate_holds_on_at_least_two_of_three_seeded_refits(featured: pd.DataFrame) -> None:
    """TRD §6 wording: the improve-or-hold gate must pass on >= 2 of 3 re-fits."""
    improved_or_held = 0
    for seed in (0, 1, 2):
        before = recalibrate(featured[featured["minute"] < 60], seed=seed)
        after = recalibrate(featured[featured["minute"] < 90], seed=seed)

        accuracy_before = float(
            accuracy_score(featured["true_archetype"], classify(featured, before)["predicted_archetype"])
        )
        accuracy_after = float(
            accuracy_score(featured["true_archetype"], classify(featured, after)["predicted_archetype"])
        )
        print(f"seed {seed}: {accuracy_before:.4f} -> {accuracy_after:.4f}")
        if accuracy_after >= accuracy_before - 1e-9:
            improved_or_held += 1

    assert improved_or_held >= 2, f"only {improved_or_held}/3 re-fits held or improved accuracy"


def test_recalibration_recovers_from_a_biased_initial_window() -> None:
    """The loop's actual purpose: a classifier trained on a narrow slice gets better.

    The first window contains almost no agentic traffic, so the initial fit is weak on
    that class; after the shift brings agentic volume in, recalibrating recovers it.
    """
    cfg = TraceConfig(duration_minutes=120, shift_start_min=30, shift_duration_min=10, seed=5)
    featured = add_proxy_features(generate_trace(cfg), seed=5)

    early = recalibrate(featured[featured["minute"] < 30], seed=0)
    late = recalibrate(featured[featured["minute"] < 90], seed=0)

    post_shift = featured[featured["minute"] >= 40]
    recall_early = float(
        (classify(post_shift, early)["predicted_archetype"] == post_shift["true_archetype"]).mean()
    )
    recall_late = float(
        (classify(post_shift, late)["predicted_archetype"] == post_shift["true_archetype"]).mean()
    )
    print(f"post-shift accuracy: early-window fit {recall_early:.4f} -> expanded fit {recall_late:.4f}")
    assert recall_late >= recall_early - 1e-9
