"""Tests for the request-level latency model — Addendum §9.1, §9.4 and the §13
"Latency model" acceptance gate, (a) through (e).
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from src.classifier.archetype_classifier import add_proxy_features, classify, train_classifier
from src.config import ARCHETYPES, EVALUATION_DEFAULTS, PRIMITIVE_MAP
from src.controller.archetype_aware_policy import archetype_aware_scaling_decision
from src.controller.baseline_policy import BASELINE_POOL, baseline_scaling_decision
from src.evaluation.latency_model import (
    ARRIVAL_SEED,
    BASE_SERVICE_S,
    INITIAL_UNITS_AWARE_PER_PRIMITIVE,
    SLOTS_PER_UNIT,
    arrival_seconds,
    capacity_timeline,
    service_seconds,
    simulate_policy_latency,
    simulate_pool,
    summarise_latency,
)
from src.forecaster.per_archetype_forecaster import forecast_aggregate, forecast_all_archetypes
from src.trace_gen.generator import _default_config, generate_trace

HORIZON = int(EVALUATION_DEFAULTS["forecast_horizon"])
START_MINUTE = int(EVALUATION_DEFAULTS["start_minute"])
PER_UNIT_CAPACITY = int(EVALUATION_DEFAULTS["baseline_per_unit_capacity"])


@pytest.fixture(scope="module")
def labeled() -> pd.DataFrame:
    """The same trace/seed run_comparison.simulate() uses, before any recalibration."""
    cfg = _default_config()
    featured = add_proxy_features(generate_trace(cfg), seed=cfg.seed)
    clf = train_classifier(featured, seed=cfg.seed)
    return classify(featured, clf)


@pytest.fixture(scope="module")
def results(labeled: pd.DataFrame) -> pd.DataFrame:
    """Per-minute capacity decisions driven by the SAME (non-recalibrated) ``labeled``.

    Deliberately not ``run_comparison.run()``: that recalibrates the classifier
    mid-run, so its capacity decisions are driven by a different, evolving prediction
    stream than any single fixed ``labeled`` frame. The latency model replays requests
    against a capacity timeline, so the two must be derived from the same predictions.
    """
    cfg = _default_config()
    baseline_capacity = 1
    aware_capacity: dict[str, int] = {}
    rows: list[dict] = []
    for minute in range(START_MINUTE, cfg.duration_minutes):
        window = labeled[labeled["minute"] <= minute]
        baseline_capacity = baseline_scaling_decision(
            forecast_aggregate(window, HORIZON), baseline_capacity, PER_UNIT_CAPACITY
        )["new_capacity"]
        for decision in archetype_aware_scaling_decision(
            forecast_all_archetypes(window, HORIZON), aware_capacity
        ):
            aware_capacity[decision["target_pool"]] = decision["new_capacity"]
        row = {"minute": minute, "baseline_capacity": int(baseline_capacity)}
        for primitive in PRIMITIVE_MAP.values():
            row[f"aware_capacity_{primitive}"] = int(aware_capacity.get(primitive, 0))
        rows.append(row)
    return pd.DataFrame(rows)


# ================================================================================
# BASE_SERVICE_S — derived, not hardcoded (Addendum §9.1)
# ================================================================================

def test_base_service_s_matches_the_addendum_derived_values() -> None:
    expected = {
        "short_conversational": 4.0,
        "long_context_rag": 8.0,
        "agentic_tool_using": 12.0,
        "batch_offline": 10.0,
    }
    assert set(BASE_SERVICE_S) == set(ARCHETYPES)
    for archetype, value in expected.items():
        assert BASE_SERVICE_S[archetype] == pytest.approx(value)


# ================================================================================
# service_seconds / arrival_seconds
# ================================================================================

def test_service_seconds_requires_its_columns() -> None:
    with pytest.raises(KeyError, match="service_seconds"):
        service_seconds(pd.DataFrame({"true_archetype": ["short_conversational"]}))


def test_service_seconds_is_positive_and_archetype_scaled(labeled: pd.DataFrame) -> None:
    service = service_seconds(labeled)
    assert (service > 0.0).all()
    # agentic_tool_using has the largest base service time (12s) and the largest
    # COMPUTE_COST_BASE, so its mean service time should clearly exceed short_conversational's.
    by_archetype = service.groupby(labeled["true_archetype"]).mean()
    assert by_archetype["agentic_tool_using"] > by_archetype["short_conversational"]


def test_arrival_seconds_requires_its_columns() -> None:
    with pytest.raises(KeyError, match="arrival_seconds"):
        arrival_seconds(pd.DataFrame({"minute": [0]}), seed=1)


def test_arrival_seconds_lands_within_the_request_minute(labeled: pd.DataFrame) -> None:
    arrival = arrival_seconds(labeled, seed=ARRIVAL_SEED)
    offsets = arrival.to_numpy() - labeled["minute"].to_numpy() * 60.0
    assert (offsets >= 0.0).all()
    assert (offsets < 60.0).all()


def test_arrival_seconds_same_seed_is_deterministic(labeled: pd.DataFrame) -> None:
    first = arrival_seconds(labeled, seed=ARRIVAL_SEED)
    second = arrival_seconds(labeled, seed=ARRIVAL_SEED)
    pd.testing.assert_series_equal(first, second)


# ================================================================================
# §13 Latency model gate (a): conservation — arrival <= start <= end, nothing lost
# ================================================================================

def test_a_conservation_every_request_is_served_and_never_starts_early(
    labeled: pd.DataFrame, results: pd.DataFrame
) -> None:
    per_request = simulate_policy_latency(labeled, results, "aware", seed=ARRIVAL_SEED)
    assert len(per_request) == len(labeled), "every request must be served; none lost"
    assert (per_request["wait_s"] >= -1e-9).all(), "a request must not start before it arrives"
    assert (per_request["response_s"] >= per_request["wait_s"] - 1e-9).all(), (
        "end time must not precede start time"
    )


# ================================================================================
# §13 Latency model gate (b): busy slots never exceed the allowed count
# ================================================================================

def test_b_busy_slots_never_exceed_the_allowed_slots(
    labeled: pd.DataFrame, results: pd.DataFrame
) -> None:
    """Reconstruct the busy count at every start and end event for each pool; it must
    never exceed slots_per_unit * units(pool, minute) at that instant.
    """
    per_request = simulate_policy_latency(labeled, results, "aware", seed=ARRIVAL_SEED)
    duration_minutes = int(labeled["minute"].max()) + 1
    timeline = capacity_timeline(results, "aware", duration_minutes)

    for pool in sorted(set(PRIMITIVE_MAP.values())):
        pool_rows = per_request[per_request["pool"] == pool]
        start_s = (pool_rows["arrival_s"] + pool_rows["wait_s"]).to_numpy()
        end_s = (pool_rows["arrival_s"] + pool_rows["response_s"]).to_numpy()
        # ends (-1) sort before starts (+1) at an identical timestamp, so a slot freed
        # at exactly t is already free for a request starting at t.
        events = sorted([(t, 1) for t in start_s] + [(t, -1) for t in end_s])

        busy = 0
        for t, delta in events:
            busy += delta
            if delta == 1:
                minute = min(int(t // 60.0), duration_minutes - 1)
                allowed = SLOTS_PER_UNIT * timeline[pool][minute]
                assert busy <= allowed, f"{pool}: busy {busy} > allowed {allowed} at t={t}"


# ================================================================================
# §13 Latency model gate (c): deterministic single-slot waits match by hand
# ================================================================================

def test_c_single_slot_matches_hand_computed_waits() -> None:
    arrival_s = np.array([0.0, 1.0, 2.0])
    service_s = np.array([5.0, 5.0, 5.0])
    result = simulate_pool(arrival_s, service_s, {0: 1})

    np.testing.assert_allclose(result["start_s"].to_numpy(), [0.0, 5.0, 10.0])
    np.testing.assert_allclose(result["end_s"].to_numpy(), [5.0, 10.0, 15.0])
    np.testing.assert_allclose(result["wait_s"].to_numpy(), [0.0, 4.0, 8.0])
    np.testing.assert_allclose(result["response_s"].to_numpy(), [5.0, 9.0, 13.0])


def test_c_two_slots_serve_two_requests_concurrently() -> None:
    arrival_s = np.array([0.0, 0.0, 0.0])
    service_s = np.array([5.0, 5.0, 5.0])
    result = simulate_pool(arrival_s, service_s, {0: 2})

    np.testing.assert_allclose(result["start_s"].to_numpy(), [0.0, 0.0, 5.0])
    np.testing.assert_allclose(result["wait_s"].to_numpy(), [0.0, 0.0, 5.0])


# ================================================================================
# §13 Latency model gate (d): M/M/1 — measured mean wait vs the analytic formula
# ================================================================================

def test_d_mm1_mean_wait_matches_the_analytic_formula() -> None:
    """rho = 0.5, n >= 20,000; mean wait within 5% of rho / (mu - lambda)."""
    rng = np.random.default_rng(2024)
    n = 20_000
    mu = 1.0  # service rate
    lam = 0.5  # arrival rate -> rho = 0.5
    rho = lam / mu

    interarrival = rng.exponential(1.0 / lam, size=n)
    arrival_s = np.cumsum(interarrival)
    service_s = rng.exponential(1.0 / mu, size=n)

    last_minute = int(arrival_s[-1] // 60.0) + 2
    slots_by_minute = {minute: 1 for minute in range(last_minute)}

    result = simulate_pool(arrival_s, service_s, slots_by_minute)
    measured_wait = float(result["wait_s"].mean())
    analytic_wait = rho / (mu - lam)
    relative_error = abs(measured_wait - analytic_wait) / analytic_wait

    print(
        f"\nM/M/1 (rho={rho}): measured mean wait = {measured_wait:.4f}s, "
        f"analytic rho/(mu-lambda) = {analytic_wait:.4f}s, relative error = {relative_error:.2%}"
    )
    assert relative_error < 0.05


# ================================================================================
# §13 Latency model gate (e): same seed -> identical output
# ================================================================================

def test_e_same_seed_gives_identical_output(labeled: pd.DataFrame, results: pd.DataFrame) -> None:
    first = simulate_policy_latency(labeled, results, "aware", seed=ARRIVAL_SEED)
    second = simulate_policy_latency(labeled, results, "aware", seed=ARRIVAL_SEED)
    pd.testing.assert_frame_equal(first, second)


# ================================================================================
# simulate_pool / simulate_policy_latency contract
# ================================================================================

def test_simulate_pool_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="equal length"):
        simulate_pool(np.array([0.0, 1.0]), np.array([1.0]), {0: 1})


def test_simulate_pool_rejects_empty_slots_by_minute() -> None:
    with pytest.raises(ValueError, match="slots_by_minute"):
        simulate_pool(np.array([0.0]), np.array([1.0]), {})


def test_simulate_policy_latency_rejects_unknown_policy(
    labeled: pd.DataFrame, results: pd.DataFrame
) -> None:
    with pytest.raises(ValueError, match="unknown policy"):
        simulate_policy_latency(labeled, results, "both", seed=ARRIVAL_SEED)  # type: ignore[arg-type]


def test_aware_routes_by_prediction_and_charges_service_by_truth(
    labeled: pd.DataFrame, results: pd.DataFrame
) -> None:
    per_request = simulate_policy_latency(labeled, results, "aware", seed=ARRIVAL_SEED)

    expected_pool = per_request["predicted_archetype"].map(PRIMITIVE_MAP)
    assert (per_request["pool"] == expected_pool).all()

    merged = per_request.merge(labeled[["request_id", "compute_cost"]], on="request_id")
    expected_service = service_seconds(merged[["true_archetype", "compute_cost"]])
    np.testing.assert_allclose(merged["service_s"].to_numpy(), expected_service.to_numpy())


def test_baseline_routes_everything_to_the_single_homogeneous_pool(
    labeled: pd.DataFrame, results: pd.DataFrame
) -> None:
    per_request = simulate_policy_latency(labeled, results, "baseline", seed=ARRIVAL_SEED)
    assert (per_request["pool"] == BASELINE_POOL).all()


def test_capacity_timeline_uses_the_initial_units_before_the_first_decision(
    results: pd.DataFrame,
) -> None:
    duration_minutes = int(results["minute"].max()) + 1
    start_minute = int(results["minute"].min())
    timeline = capacity_timeline(results, "aware", duration_minutes)
    by_minute = results.set_index("minute")

    for primitive in PRIMITIVE_MAP.values():
        assert timeline[primitive][0] == INITIAL_UNITS_AWARE_PER_PRIMITIVE
        assert timeline[primitive][start_minute] == int(
            by_minute.loc[start_minute, f"aware_capacity_{primitive}"]
        )


def test_capacity_timeline_rejects_unknown_policy(results: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="unknown policy"):
        capacity_timeline(results, "both", 120)  # type: ignore[arg-type]


# ================================================================================
# summarise_latency
# ================================================================================

def test_summarise_latency_is_json_serialisable_and_covers_every_window(
    labeled: pd.DataFrame, results: pd.DataFrame
) -> None:
    cfg = _default_config()
    per_request = simulate_policy_latency(labeled, results, "aware", seed=ARRIVAL_SEED)
    summary = summarise_latency(
        per_request, cfg.shift_start_min, cfg.shift_start_min + cfg.shift_duration_min
    )

    json.dumps(summary)  # raises TypeError if anything is not JSON-serialisable
    assert set(summary) == {"overall", "by_archetype", "by_window"}
    assert set(summary["by_window"]) == {"pre", "during", "post"}
    assert set(summary["by_archetype"]) == set(ARCHETYPES)
    assert summary["overall"]["n"] == len(per_request)
