"""Tests for the forecasters — TRD §1.4, §2 and the §6 "Forecaster" divergence gate."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.classifier.archetype_classifier import add_proxy_features, classify, train_classifier
from src.config import ARCHETYPES
from src.forecaster.per_archetype_forecaster import (
    AGGREGATE_COLUMN,
    build_minute_series,
    forecast_aggregate,
    forecast_all_archetypes,
    forecast_series,
)
from src.trace_gen.generator import _default_config, generate_trace

HORIZON = 5


@pytest.fixture(scope="module")
def labeled() -> pd.DataFrame:
    featured = add_proxy_features(generate_trace(_default_config()), seed=0)
    return classify(featured, train_classifier(featured, seed=0))


# --------------------------------------------------------------------------------
# build_minute_series
# --------------------------------------------------------------------------------

def test_build_minute_series_per_archetype_shape(labeled: pd.DataFrame) -> None:
    series = build_minute_series(labeled, "predicted_archetype")
    assert list(series.columns) == ARCHETYPES
    assert list(series.index) == list(range(int(labeled["minute"].max()) + 1))
    assert series.index.name == "minute"


def test_build_minute_series_aggregate_shape(labeled: pd.DataFrame) -> None:
    series = build_minute_series(labeled, None)
    assert list(series.columns) == [AGGREGATE_COLUMN]
    assert len(series) == int(labeled["minute"].max()) + 1


def test_per_archetype_rows_sum_to_the_aggregate(labeled: pd.DataFrame) -> None:
    per_archetype = build_minute_series(labeled, "predicted_archetype")
    aggregate = build_minute_series(labeled, None)[AGGREGATE_COLUMN]
    pd.testing.assert_series_equal(
        per_archetype.sum(axis=1), aggregate, check_names=False
    )


def test_missing_minutes_are_filled_with_zero() -> None:
    """Gaps become explicit zeros so the smoother never sees a hole."""
    sparse = pd.DataFrame(
        {"minute": [0, 0, 4], "predicted_archetype": ["short_conversational"] * 3}
    )
    series = build_minute_series(sparse, "predicted_archetype")
    assert list(series.index) == [0, 1, 2, 3, 4]
    assert series.loc[1:3].to_numpy().sum() == 0.0
    assert series.loc[0, "short_conversational"] == 2.0


def test_build_minute_series_rejects_bad_input(labeled: pd.DataFrame) -> None:
    """NFR-3: missing columns and empty frames raise instead of returning junk."""
    with pytest.raises(KeyError, match="minute"):
        build_minute_series(pd.DataFrame({"x": [1]}), None)
    with pytest.raises(KeyError, match="nope"):
        build_minute_series(labeled, "nope")
    with pytest.raises(ValueError, match="empty"):
        build_minute_series(labeled.head(0), None)


# --------------------------------------------------------------------------------
# forecast_series edge cases
# --------------------------------------------------------------------------------

def test_all_zero_series_returns_zeros() -> None:
    """The explicit edge case from the T3.1 requirements — no smoother error."""
    zeros = pd.Series(np.zeros(20), index=pd.Index(range(20), name="minute"))
    forecast = forecast_series(zeros, horizon=HORIZON)
    assert len(forecast) == HORIZON
    assert (forecast == 0.0).all()


def test_constant_series_forecasts_that_constant() -> None:
    constant = pd.Series(np.full(20, 7.0), index=pd.Index(range(20), name="minute"))
    assert forecast_series(constant, horizon=HORIZON).eq(7.0).all()


def test_very_short_series_holds_last_value() -> None:
    short = pd.Series([3.0, 9.0], index=pd.Index([0, 1], name="minute"))
    assert forecast_series(short, horizon=3).eq(9.0).all()


def test_forecast_index_continues_the_series(labeled: pd.DataFrame) -> None:
    series = build_minute_series(labeled, None)[AGGREGATE_COLUMN]
    forecast = forecast_series(series, horizon=HORIZON)
    last = int(series.index[-1])
    assert list(forecast.index) == list(range(last + 1, last + 1 + HORIZON))


def test_forecast_is_never_negative(labeled: pd.DataFrame) -> None:
    declining = pd.Series(np.linspace(40.0, 0.5, 40), index=pd.Index(range(40), name="minute"))
    assert (forecast_series(declining, horizon=20) >= 0.0).all()


def test_invalid_horizon_raises(labeled: pd.DataFrame) -> None:
    series = build_minute_series(labeled, None)[AGGREGATE_COLUMN]
    with pytest.raises(ValueError, match="horizon"):
        forecast_series(series, horizon=0)


def test_forecast_series_is_deterministic(labeled: pd.DataFrame) -> None:
    """NFR-2: no stochastic component, so repeated calls must agree exactly."""
    series = build_minute_series(labeled, "predicted_archetype")["agentic_tool_using"]
    pd.testing.assert_series_equal(forecast_series(series, HORIZON), forecast_series(series, HORIZON))


# --------------------------------------------------------------------------------
# wrappers
# --------------------------------------------------------------------------------

def test_forecast_all_archetypes_returns_the_trd_forecast_vector(labeled: pd.DataFrame) -> None:
    """TRD §1.4: one key per archetype, each a Series of length `horizon`."""
    forecasts = forecast_all_archetypes(labeled, horizon=HORIZON)
    assert set(forecasts) == set(ARCHETYPES)
    for archetype, series in forecasts.items():
        assert isinstance(series, pd.Series)
        assert len(series) == HORIZON
        assert (series >= 0).all()


def test_forecast_aggregate_returns_a_single_series(labeled: pd.DataFrame) -> None:
    forecast = forecast_aggregate(labeled, horizon=HORIZON)
    assert isinstance(forecast, pd.Series)
    assert len(forecast) == HORIZON


def test_per_archetype_path_uses_predicted_not_true_archetype(labeled: pd.DataFrame) -> None:
    """Scrambling ground truth must not move the forecast — it is never read."""
    baseline = forecast_all_archetypes(labeled, horizon=HORIZON)
    scrambled = labeled.copy()
    rng = np.random.default_rng(0)
    scrambled["true_archetype"] = rng.permutation(scrambled["true_archetype"].to_numpy())
    for archetype, series in forecast_all_archetypes(scrambled, horizon=HORIZON).items():
        pd.testing.assert_series_equal(series, baseline[archetype])


# --------------------------------------------------------------------------------
# TRD §6 acceptance gate: per-archetype sees the shift, aggregate does not
# --------------------------------------------------------------------------------

def _pre_shift_baselines(labeled: pd.DataFrame, shift_start: int) -> tuple[float, float]:
    pre = labeled[labeled["minute"] < shift_start]
    agentic = float(build_minute_series(pre, "predicted_archetype")["agentic_tool_using"].mean())
    aggregate = float(build_minute_series(pre, None)[AGGREGATE_COLUMN].mean())
    return agentic, aggregate


def test_aggregate_forecast_stays_blind_to_the_shift(labeled: pd.DataFrame) -> None:
    """The archetype-agnostic signal must NOT move — that is what the baseline sees."""
    cfg = _default_config()
    _, aggregate_baseline = _pre_shift_baselines(labeled, cfg.shift_start_min)
    for window_end in (62, 65, 68, 70, 75, 80):
        window = labeled[labeled["minute"] <= window_end]
        forecast = float(forecast_aggregate(window, horizon=HORIZON).mean())
        relative_change = abs(forecast / aggregate_baseline - 1.0)
        assert relative_change < 0.10, (
            f"aggregate forecast moved {relative_change:.1%} at minute {window_end}; "
            "the shift is leaking into the aggregate signal"
        )


def test_per_archetype_forecast_detects_the_shift(labeled: pd.DataFrame) -> None:
    """TRD §6 gate: the shifting archetype's forecast rises well above its pre-shift level."""
    cfg = _default_config()
    agentic_baseline, _ = _pre_shift_baselines(labeled, cfg.shift_start_min)
    window = labeled[labeled["minute"] <= 70]
    forecast = float(forecast_all_archetypes(window, horizon=HORIZON)["agentic_tool_using"].mean())
    assert forecast > agentic_baseline * 1.5, (
        f"agentic forecast {forecast:.2f} did not rise above its pre-shift baseline "
        f"{agentic_baseline:.2f} ten minutes into the shift"
    )


def test_per_archetype_forecast_is_quiet_before_the_shift(labeled: pd.DataFrame) -> None:
    """No false positives: the detector must be still while nothing is happening."""
    cfg = _default_config()
    agentic_baseline, _ = _pre_shift_baselines(labeled, cfg.shift_start_min)
    for window_end in (40, 45, 50, 55, 58):
        window = labeled[labeled["minute"] <= window_end]
        forecast = float(forecast_all_archetypes(window, horizon=HORIZON)["agentic_tool_using"].mean())
        assert abs(forecast / agentic_baseline - 1.0) < 0.10


def test_per_archetype_detects_the_shift_before_the_aggregate_does(labeled: pd.DataFrame) -> None:
    """The headline forecaster result: composition-aware detection, aggregate-blind baseline.

    Detection = forecast exceeds its own pre-shift baseline by more than 25%, comfortably
    above the measured pre-shift noise floor of ~3%.
    """
    cfg = _default_config()
    agentic_baseline, aggregate_baseline = _pre_shift_baselines(labeled, cfg.shift_start_min)
    threshold = 0.25

    agentic_detected_at: int | None = None
    aggregate_detected_at: int | None = None
    for window_end in range(cfg.shift_start_min, int(labeled["minute"].max()) + 1):
        window = labeled[labeled["minute"] <= window_end]
        if agentic_detected_at is None:
            agentic = float(forecast_all_archetypes(window, horizon=HORIZON)["agentic_tool_using"].mean())
            if agentic / agentic_baseline - 1.0 > threshold:
                agentic_detected_at = window_end
        if aggregate_detected_at is None:
            aggregate = float(forecast_aggregate(window, horizon=HORIZON).mean())
            if aggregate / aggregate_baseline - 1.0 > threshold:
                aggregate_detected_at = window_end
        if agentic_detected_at is not None and aggregate_detected_at is not None:
            break

    assert agentic_detected_at is not None, "per-archetype forecaster never detected the shift"
    assert aggregate_detected_at is None, (
        f"aggregate forecaster detected the shift at minute {aggregate_detected_at}; "
        "it should stay blind to a pure composition change"
    )
    lead_minutes = agentic_detected_at - cfg.shift_start_min
    print(
        f"\nshift starts at minute {cfg.shift_start_min}; per-archetype forecaster detects it at "
        f"minute {agentic_detected_at} (+{lead_minutes} min); aggregate-only forecaster never does."
    )
    assert lead_minutes <= 10
