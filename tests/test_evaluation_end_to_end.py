"""End-to-end evaluation tests — TRD §1.8, §2 and the §6 "Evaluation" gate.

This is the project's headline result, so these tests run the real `run()` once and
assert against its actual output files rather than a re-implementation.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.config import EVALUATION_DEFAULTS, PRIMITIVE_MAP
from src.evaluation.run_comparison import (
    PLOT_PATH,
    RESULTS_PATH,
    _blended_unit_cost,
    run,
)
from src.trace_gen.generator import _default_config

# The five EvaluationRow columns TRD §1.8 requires by these exact names.
REQUIRED_COLUMNS = [
    "minute",
    "baseline_capacity",
    "aware_capacity_total",
    "baseline_projected_spend",
    "aware_projected_spend",
]


@pytest.fixture(scope="module")
def results() -> pd.DataFrame:
    """Run the full evaluation once and return the CSV it actually wrote."""
    run()
    assert RESULTS_PATH.is_file(), f"{RESULTS_PATH} was not written"
    assert PLOT_PATH.is_file(), f"{PLOT_PATH} was not written"
    return pd.read_csv(RESULTS_PATH)


def test_run_writes_both_output_files(results: pd.DataFrame) -> None:
    assert RESULTS_PATH.stat().st_size > 0
    assert PLOT_PATH.stat().st_size > 0


def test_csv_has_all_five_required_columns(results: pd.DataFrame) -> None:
    """TRD §1.8: these five must always be present under these exact names."""
    for column in REQUIRED_COLUMNS:
        assert column in results.columns, f"required EvaluationRow column '{column}' is missing"
    assert pd.api.types.is_integer_dtype(results["minute"])
    assert pd.api.types.is_integer_dtype(results["baseline_capacity"])
    assert pd.api.types.is_integer_dtype(results["aware_capacity_total"])
    assert pd.api.types.is_float_dtype(results["baseline_projected_spend"])
    assert pd.api.types.is_float_dtype(results["aware_projected_spend"])


def test_run_covers_the_expected_minutes(results: pd.DataFrame) -> None:
    cfg = _default_config()
    start = int(EVALUATION_DEFAULTS["start_minute"])
    assert results["minute"].min() == start
    assert results["minute"].max() == cfg.duration_minutes - 1
    assert results["minute"].is_monotonic_increasing
    assert len(results) == cfg.duration_minutes - start


def test_capacities_are_always_valid(results: pd.DataFrame) -> None:
    assert (results["baseline_capacity"] >= 1).all(), "baseline capacity dropped below 1"
    assert (results["aware_capacity_total"] >= 1).all()
    assert (results["baseline_projected_spend"] >= 0).all()
    assert (results["aware_projected_spend"] >= 0).all()


def test_per_primitive_breakdown_sums_to_the_total(results: pd.DataFrame) -> None:
    columns = [f"aware_capacity_{p}" for p in PRIMITIVE_MAP.values()]
    for column in columns:
        assert column in results.columns
    pd.testing.assert_series_equal(
        results[columns].sum(axis=1), results["aware_capacity_total"], check_names=False
    )


# --------------------------------------------------------------------------------
# TRD §6 "Evaluation" gate — the project's headline result
# --------------------------------------------------------------------------------

def test_capacity_trajectories_diverge_after_the_shift(results: pd.DataFrame) -> None:
    """THE GATE: aware capacity diverges from baseline (normalised) after shift_start_min.

    Both trajectories are normalised by their own pre-shift mean, so this measures a
    change in behaviour rather than the policies' different unit scales.
    """
    cfg = _default_config()
    indexed = results.set_index("minute")
    pre = indexed.loc[: cfg.shift_start_min - 1]
    post = indexed.loc[cfg.shift_start_min :]

    baseline_ratio = post["baseline_capacity"].mean() / pre["baseline_capacity"].mean()
    aware_ratio = post["aware_capacity_total"].mean() / pre["aware_capacity_total"].mean()
    divergence = abs(aware_ratio - baseline_ratio)

    print(
        f"\nnormalised post/pre capacity: baseline {baseline_ratio:.4f}, aware {aware_ratio:.4f}, "
        f"divergence {divergence:.4f}"
    )
    assert divergence > 0.15, f"capacity trajectories did not diverge (gap {divergence:.4f})"
    assert aware_ratio > 1.10, "archetype-aware policy failed to scale up after the shift"
    assert abs(baseline_ratio - 1.0) < 0.15, "baseline moved; it should be blind to the shift"


def test_the_extra_capacity_goes_to_the_gpu_reserved_pool(results: pd.DataFrame) -> None:
    """The aware policy must scale the RIGHT primitive, not just scale up in general."""
    cfg = _default_config()
    indexed = results.set_index("minute")
    pre = indexed.loc[: cfg.shift_start_min - 1]
    post = indexed.loc[cfg.shift_start_min :]

    reserved_growth = post["aware_capacity_eks_gpu_reserved"].mean() - pre["aware_capacity_eks_gpu_reserved"].mean()
    assert reserved_growth > 0.5, "eks_gpu_reserved did not grow after the agentic shift"

    for primitive in ("bedrock_on_demand", "sagemaker_endpoint", "eks_gpu_spot"):
        column = f"aware_capacity_{primitive}"
        drift = abs(post[column].mean() - pre[column].mean())
        assert drift < reserved_growth, f"{primitive} moved as much as the shifting primitive"


def test_aware_governance_detects_the_budget_breach_first(results: pd.DataFrame) -> None:
    """PRD §8 secondary metric: positive lead time over the aggregate-only view."""
    aware_breaches = results[results["aware_breach_projected"]]
    baseline_breaches = results[results["baseline_breach_projected"]]

    assert not aware_breaches.empty, "archetype-aware governance never projected a breach"
    aware_at = int(aware_breaches["minute"].iloc[0])

    if baseline_breaches.empty:
        print(f"\naware detects the projected breach at minute {aware_at}; aggregate-only never does")
        return

    baseline_at = int(baseline_breaches["minute"].iloc[0])
    lead = baseline_at - aware_at
    print(f"\nbreach projected: aware at minute {aware_at}, aggregate-only at minute {baseline_at} "
          f"-> lead time {lead} minutes")
    assert lead > 0, "archetype-aware governance had no lead time over the aggregate-only view"


def test_aware_governance_reacts_to_the_shift_not_to_drift(results: pd.DataFrame) -> None:
    """The aware breach must be triggered by the shift, not by spend slowly accumulating."""
    cfg = _default_config()
    aware_breaches = results[results["aware_breach_projected"]]
    assert not aware_breaches.empty
    aware_at = int(aware_breaches["minute"].iloc[0])
    assert aware_at >= cfg.shift_start_min, "aware projected a breach before the shift even began"
    assert aware_at - cfg.shift_start_min <= 20


def test_budget_actions_are_valid_values(results: pd.DataFrame) -> None:
    valid = {"none", "downgrade_tier", "throttle"}
    assert set(results["aware_budget_action"].unique()) <= valid
    assert set(results["baseline_budget_action"].unique()) <= valid
    # An action fires exactly when a breach is projected, and never otherwise.
    assert (results.loc[~results["aware_breach_projected"], "aware_budget_action"] == "none").all()
    assert (results.loc[results["aware_breach_projected"], "aware_budget_action"] != "none").all()


def test_blended_unit_cost_is_between_the_cheapest_and_dearest_primitive() -> None:
    """The baseline's blended rate must be a genuine blend, not a hidden advantage."""
    from src.config import PRIMITIVE_UNIT_CAPACITY, UNIT_COST_PER_MIN

    per_unit_capacity = int(EVALUATION_DEFAULTS["baseline_per_unit_capacity"])
    blended_per_request = _blended_unit_cost(per_unit_capacity) / per_unit_capacity
    rates = [UNIT_COST_PER_MIN[p] / PRIMITIVE_UNIT_CAPACITY[p] for p in PRIMITIVE_MAP.values()]
    assert min(rates) < blended_per_request < max(rates)


def test_run_is_deterministic(results: pd.DataFrame) -> None:
    """NFR-2: re-running the whole evaluation reproduces the CSV exactly."""
    run()
    pd.testing.assert_frame_equal(pd.read_csv(RESULTS_PATH), results)
