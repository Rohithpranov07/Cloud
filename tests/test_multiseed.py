"""Tests for the 30-seed paired statistical evaluation — Addendum §10.1 and the §13
"Multi-seed" acceptance gate.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy.stats import wilcoxon

from src.evaluation.multiseed import (
    METRICS,
    _bootstrap_median_ci,
    _rank_biserial,
    paired_tests,
    run_all,
    run_seed,
)
from src.evaluation.run_comparison import POLICIES, SIZING_RULES

EXPECTED_ROW_KEYS = {
    "seed",
    "sizing",
    "detect_minute",
    "aware_scaleup_minute",
    "aware_breach_minute",
    "baseline_breach_minute",
    "governance_lead_min",
    *(f"{policy}_{metric}" for policy in POLICIES for metric in METRICS),
    *(f"{policy}_actual_spend" for policy in POLICIES),
}


def test_run_seed_returns_the_expected_keys() -> None:
    row = run_seed(0, "round")
    assert set(row) == EXPECTED_ROW_KEYS
    assert row["seed"] == 0
    assert row["sizing"] == "round"


def test_run_seed_a_metric_that_never_occurs_is_nan() -> None:
    """Under round, the baseline's post-shift SLA-violation rate is ~1.0 (Addendum §8.1),
    so it has zero compliant requests and cost_per_compliant_request must be NaN, not 0.
    """
    row = run_seed(0, "round")
    if row["baseline_sla_violation_rate"] >= 1.0:
        assert np.isnan(row["baseline_cost_per_compliant_request"])


def test_run_all_workers_1_matches_workers_2() -> None:
    seeds = [0, 1]
    single = run_all(seeds, sizings=("round",), workers=1)
    parallel = run_all(seeds, sizings=("round",), workers=2)
    pd.testing.assert_frame_equal(
        single.reset_index(drop=True), parallel.reset_index(drop=True)
    )


# --------------------------------------------------------------------------------
# paired_tests — on a small synthetic frame, so the statistics can be hand-verified
# --------------------------------------------------------------------------------


def _synthetic_df() -> pd.DataFrame:
    rows: list[dict] = []
    rng = np.random.default_rng(7)
    for sizing in SIZING_RULES:
        for seed in range(10):
            baseline_sla = float(rng.uniform(0.5, 1.0))
            aware_sla = baseline_sla - float(rng.uniform(0.1, 0.4))  # consistently lower
            rows.append(
                {
                    "seed": seed,
                    "sizing": sizing,
                    "baseline_sla_violation_rate": baseline_sla,
                    "aware_sla_violation_rate": aware_sla,
                    "baseline_p95_response_s": float(rng.uniform(50, 100)),
                    "aware_p95_response_s": float(rng.uniform(10, 40)),
                    "baseline_cost_per_compliant_request": float(rng.uniform(1, 2)),
                    "aware_cost_per_compliant_request": float(rng.uniform(0.5, 1)),
                }
            )
    return pd.DataFrame(rows)


def test_paired_tests_matches_scipy_directly_on_synthetic_data() -> None:
    df = _synthetic_df()
    summary = paired_tests(df)

    for sizing in SIZING_RULES:
        subset = df[df["sizing"] == sizing]
        b = subset["baseline_sla_violation_rate"].to_numpy()
        a = subset["aware_sla_violation_rate"].to_numpy()
        expected = wilcoxon(b, a, alternative="two-sided")

        result = summary[sizing]["sla_violation_rate"]
        assert result["n"] == len(b)
        assert result["p_value"] == pytest.approx(float(expected.pvalue))
        assert result["median_baseline"] == pytest.approx(float(np.median(b)))
        assert result["median_aware"] == pytest.approx(float(np.median(a)))
        assert result["median_paired_diff"] == pytest.approx(float(np.median(a - b)))
        assert result["ci_low"] <= result["median_paired_diff"] <= result["ci_high"]
        # aware is consistently lower by construction -> all diffs negative -> r = -1.
        assert result["rank_biserial_r"] == pytest.approx(-1.0)


def test_paired_tests_drops_nan_pairs_and_reports_n_used() -> None:
    df = _synthetic_df()
    df.loc[df.index[0], "aware_p95_response_s"] = float("nan")
    sizing = df["sizing"].iloc[0]
    expected_n = int((df["sizing"] == sizing).sum()) - 1

    summary = paired_tests(df)
    assert summary[sizing]["p95_response_s"]["n"] == expected_n


def test_paired_tests_all_nan_metric_is_nan_not_an_error() -> None:
    df = _synthetic_df()
    df["aware_cost_per_compliant_request"] = float("nan")
    summary = paired_tests(df)
    for sizing in SIZING_RULES:
        result = summary[sizing]["cost_per_compliant_request"]
        assert result["n"] == 0
        assert np.isnan(result["p_value"])
        assert np.isnan(result["median_paired_diff"])


def test_rank_biserial_all_positive_differences_is_one() -> None:
    assert _rank_biserial(np.array([1.0, 2.0, 3.0])) == pytest.approx(1.0)


def test_rank_biserial_all_negative_differences_is_minus_one() -> None:
    assert _rank_biserial(np.array([-1.0, -2.0, -3.0])) == pytest.approx(-1.0)


def test_rank_biserial_empty_after_dropping_zeros_is_nan() -> None:
    assert np.isnan(_rank_biserial(np.array([0.0, 0.0])))


def test_bootstrap_median_ci_is_deterministic_for_a_fixed_seed() -> None:
    diff = np.array([1.0, 2.0, 3.0, -1.0, 0.5])
    first = _bootstrap_median_ci(diff, seed=42, n_resamples=500)
    second = _bootstrap_median_ci(diff, seed=42, n_resamples=500)
    assert first == second


def test_bootstrap_median_ci_brackets_the_sample_median() -> None:
    diff = np.array([1.0, 2.0, 3.0, 4.0, 5.0, -1.0, -2.0])
    low, high = _bootstrap_median_ci(diff, seed=1, n_resamples=2000)
    assert low <= float(np.median(diff)) <= high
