"""Tests for cloud/adapters.py — T12.4 (Addendum §11.4)."""

from __future__ import annotations

import pandas as pd
import pytest

from cloud.adapters import count_item_from_dynamodb, counts_to_labeled
from src.classifier.archetype_classifier import add_proxy_features, classify, train_classifier
from src.config import ARCHETYPES
from src.forecaster.per_archetype_forecaster import AGGREGATE_COLUMN, build_minute_series
from src.trace_gen.generator import _default_config, generate_trace


@pytest.fixture(scope="module")
def labeled() -> pd.DataFrame:
    """The seed-42 labelled trace the live router reproduces (pre-recalibration model)."""
    cfg = _default_config()
    featured = add_proxy_features(generate_trace(cfg), seed=cfg.seed)
    return classify(featured, train_classifier(featured, seed=cfg.seed))


def _count_items(labeled: pd.DataFrame) -> list[dict[str, int]]:
    """What semauto-counts holds after the router has counted every request."""
    counts = labeled.groupby(["minute", "predicted_archetype"]).size().unstack(fill_value=0)
    items = []
    for minute, row in counts.iterrows():
        item = {"minute": int(minute)}
        item.update({a: int(row[a]) for a in counts.columns if int(row[a]) > 0})
        items.append(item)
    return items


@pytest.mark.parametrize("window_end", [10, 59, 60, 75, 119])
def test_counts_round_trip_to_identical_minute_series(labeled: pd.DataFrame, window_end: int) -> None:
    """counts → frame → build_minute_series == build_minute_series on the original frame."""
    window = labeled[labeled["minute"] <= window_end]
    frame = counts_to_labeled(_count_items(window))

    pd.testing.assert_frame_equal(
        build_minute_series(frame, "predicted_archetype"), build_minute_series(window, "predicted_archetype")
    )
    pd.testing.assert_frame_equal(build_minute_series(frame, None), build_minute_series(window, None))
    assert len(frame) == len(window)


def test_minutes_with_no_counts_become_zeros_through_the_forecasters_fill() -> None:
    frame = counts_to_labeled([{"minute": 0, "short_conversational": 2}, {"minute": 3, "batch_offline": 1}])
    series = build_minute_series(frame, None)[AGGREGATE_COLUMN]
    assert series.tolist() == [2.0, 0.0, 0.0, 1.0]


def test_missing_archetype_attributes_count_as_zero_and_order_is_irrelevant() -> None:
    frame = counts_to_labeled([{"minute": 5, "agentic_tool_using": 3}, {"minute": 1}])
    assert sorted(frame["minute"].tolist()) == [5, 5, 5]
    assert set(frame["predicted_archetype"]) == {"agentic_tool_using"}


def test_empty_counts_give_an_empty_frame_with_the_expected_columns() -> None:
    frame = counts_to_labeled([])
    assert list(frame.columns) == ["minute", "predicted_archetype"]
    assert len(frame) == 0


def test_negative_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="negative count"):
        counts_to_labeled([{"minute": 0, "batch_offline": -1}])


def test_count_item_from_dynamodb_parses_the_low_level_item() -> None:
    item = {
        "run_id": {"S": "aware-ceil-x"},
        "minute": {"N": "12"},
        "short_conversational": {"N": "27"},
        "agentic_tool_using": {"N": "6"},
        "expires_at": {"N": "1700604800"},
    }
    assert count_item_from_dynamodb(item) == {"minute": 12, "short_conversational": 27, "agentic_tool_using": 6}
    assert set(count_item_from_dynamodb(item)) <= {"minute", *ARCHETYPES}
