"""Tests for the trace generator — TRD §1.2, §1.3 and the §6 "Trace Generator" gate."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.config import ARCHETYPES
from src.trace_gen.generator import (
    COMPUTE_COST_BASE,
    N_TENANTS,
    TraceConfig,
    _default_config,
    generate_trace,
)

REQUIRED_COLUMNS = ["request_id", "minute", "true_archetype", "compute_cost", "tenant_id"]


@pytest.fixture(scope="module")
def trace() -> pd.DataFrame:
    return generate_trace(_default_config())


def test_schema_matches_trd_1_2(trace: pd.DataFrame) -> None:
    """Every TRD §1.2 ground-truth column is present under its exact name and type."""
    assert list(trace.columns) == REQUIRED_COLUMNS
    assert pd.api.types.is_integer_dtype(trace["request_id"])
    assert pd.api.types.is_integer_dtype(trace["minute"])
    assert pd.api.types.is_float_dtype(trace["compute_cost"])
    assert pd.api.types.is_integer_dtype(trace["tenant_id"])
    assert set(trace["true_archetype"].unique()) <= set(ARCHETYPES)


def test_request_id_is_monotonically_increasing(trace: pd.DataFrame) -> None:
    assert trace["request_id"].is_monotonic_increasing
    assert trace["request_id"].is_unique


def test_tenant_id_within_range(trace: pd.DataFrame) -> None:
    assert trace["tenant_id"].between(0, N_TENANTS - 1).all()


def test_compute_cost_is_positive_and_ordered_by_archetype(trace: pd.DataFrame) -> None:
    """Costs stay positive, and archetype cost ordering survives the 15% noise on average."""
    assert (trace["compute_cost"] > 0).all()
    means = trace.groupby("true_archetype")["compute_cost"].mean()
    assert means["short_conversational"] < means["batch_offline"]
    assert means["batch_offline"] < means["long_context_rag"]
    assert means["long_context_rag"] < means["agentic_tool_using"]
    for archetype, base in COMPUTE_COST_BASE.items():
        assert means[archetype] == pytest.approx(base, rel=0.05)


def test_aggregate_rate_stays_flat(trace: pd.DataFrame) -> None:
    """TRD §6 acceptance gate: per-minute aggregate count has std/mean < 0.3."""
    per_minute = trace.groupby("minute").size()
    ratio = per_minute.std() / per_minute.mean()
    assert ratio < 0.3, f"aggregate rate is not flat: std/mean={ratio:.4f}"


def test_shift_is_invisible_in_the_aggregate_signal() -> None:
    """The pre- and post-shift windows have statistically indistinguishable request counts.

    This is the load-bearing premise of the whole experiment (TRD §1.3): if the shift
    showed up in the aggregate, an archetype-agnostic baseline could see it too.
    """
    cfg = _default_config()
    trace = generate_trace(cfg)
    per_minute = trace.groupby("minute").size()
    shift_end = cfg.shift_start_min + cfg.shift_duration_min
    before = per_minute.loc[: cfg.shift_start_min - 1]
    after = per_minute.loc[shift_end:]
    relative_gap = abs(before.mean() - after.mean()) / before.mean()
    assert relative_gap < 0.1, f"aggregate rate moved {relative_gap:.3f} across the shift"


def test_composition_shifts_between_windows() -> None:
    """TRD §6: agentic_tool_using ramps up and short_conversational falls at the shift."""
    cfg = _default_config()
    trace = generate_trace(cfg)
    shift_end = cfg.shift_start_min + cfg.shift_duration_min
    before = trace[trace["minute"] < cfg.shift_start_min]["true_archetype"].value_counts(normalize=True)
    after = trace[trace["minute"] >= shift_end]["true_archetype"].value_counts(normalize=True)

    assert after["agentic_tool_using"] > before["agentic_tool_using"] + 0.2
    assert after["short_conversational"] < before["short_conversational"] - 0.2
    # Archetypes not involved in the shift stay put.
    assert after["long_context_rag"] == pytest.approx(before["long_context_rag"], abs=0.05)
    assert after["batch_offline"] == pytest.approx(before["batch_offline"], abs=0.05)


def test_mix_ramps_monotonically_through_the_shift_window() -> None:
    """The interpolation is a ramp, not a step: mid-window share sits between the ends."""
    cfg = _default_config()
    trace = generate_trace(cfg)
    mid = cfg.shift_start_min + cfg.shift_duration_min // 2
    window = trace[(trace["minute"] >= mid - 2) & (trace["minute"] <= mid + 2)]
    mid_share = (window["true_archetype"] == "agentic_tool_using").mean()
    assert 0.10 < mid_share < 0.40


def test_determinism_same_seed(trace: pd.DataFrame) -> None:
    """NFR-2: two runs with the same seed produce an identical DataFrame."""
    again = generate_trace(_default_config())
    pd.testing.assert_frame_equal(trace, again)


def test_different_seed_produces_different_trace(trace: pd.DataFrame) -> None:
    cfg = _default_config()
    cfg.seed = 1234
    other = generate_trace(cfg)
    assert not other["true_archetype"].equals(trace["true_archetype"].iloc[: len(other)])


def test_does_not_touch_global_numpy_random_state() -> None:
    """NFR-2: generation must not consume the global RNG stream."""
    np.random.seed(7)
    expected = np.random.random()
    np.random.seed(7)
    generate_trace(_default_config())
    assert np.random.random() == expected


def test_duration_is_respected() -> None:
    cfg = TraceConfig(duration_minutes=30, seed=3)
    trace = generate_trace(cfg)
    assert trace["minute"].min() >= 0
    assert trace["minute"].max() < 30


def test_invalid_config_raises() -> None:
    """NFR-3: bad input raises loudly rather than being silently corrected."""
    with pytest.raises(ValueError, match="sum to 1.0"):
        generate_trace(TraceConfig(mix_before={a: 0.1 for a in ARCHETYPES}))
    with pytest.raises(ValueError, match="four archetypes"):
        generate_trace(TraceConfig(mix_after={"short_conversational": 1.0}))
    with pytest.raises(ValueError, match="duration_minutes"):
        generate_trace(TraceConfig(duration_minutes=0))
    with pytest.raises(ValueError, match="base_rate_per_min"):
        generate_trace(TraceConfig(base_rate_per_min=0.0))
