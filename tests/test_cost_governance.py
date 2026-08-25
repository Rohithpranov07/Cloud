"""Tests for the cost-governance layer — TRD §1.6, §1.7, §2 and the §6 gate."""

from __future__ import annotations

import pytest

from src.config import PRIMITIVE_MAP, UNIT_COST_PER_MIN
from src.cost_governance.budget_projector import THROTTLE_THRESHOLD, check_budget, project_spend

BUDGET_RESULT_KEYS = {"breach_projected", "action", "overage"}


# --------------------------------------------------------------------------------
# project_spend
# --------------------------------------------------------------------------------

def test_project_spend_matches_the_trd_cost_table() -> None:
    assert project_spend({"eks_gpu_reserved": 2}, 10) == pytest.approx(2 * 0.09 * 10)
    assert project_spend({"bedrock_on_demand": 5}, 60) == pytest.approx(5 * 0.02 * 60)


def test_project_spend_sums_across_primitives() -> None:
    capacity = {"bedrock_on_demand": 2, "sagemaker_endpoint": 1, "eks_gpu_reserved": 3, "eks_gpu_spot": 4}
    expected = sum(units * UNIT_COST_PER_MIN[p] * 30 for p, units in capacity.items())
    assert project_spend(capacity, 30) == pytest.approx(expected)


def test_project_spend_zero_cases() -> None:
    assert project_spend({}, 60) == 0.0
    assert project_spend({"eks_gpu_reserved": 5}, 0) == 0.0
    assert project_spend({"eks_gpu_reserved": 0}, 60) == 0.0


def test_project_spend_scales_linearly_in_both_arguments() -> None:
    base = project_spend({"eks_gpu_reserved": 2}, 30)
    assert project_spend({"eks_gpu_reserved": 4}, 30) == pytest.approx(2 * base)
    assert project_spend({"eks_gpu_reserved": 2}, 60) == pytest.approx(2 * base)


def test_expensive_primitives_cost_more_for_the_same_units() -> None:
    """The cost asymmetry is what makes composition-aware governance worth anything."""
    reserved = project_spend({"eks_gpu_reserved": 4}, 60)
    bedrock = project_spend({"bedrock_on_demand": 4}, 60)
    assert reserved > bedrock * 4


def test_project_spend_rejects_invalid_input() -> None:
    """NFR-3: a typo must never silently under-report cost."""
    with pytest.raises(KeyError, match="unknown primitives"):
        project_spend({"eks_gpu_resevred": 2}, 10)
    with pytest.raises(KeyError, match="unknown primitives"):
        project_spend({"homogeneous_pool": 2}, 10)
    with pytest.raises(ValueError, match="minutes_remaining"):
        project_spend({"eks_gpu_reserved": 2}, -1)
    with pytest.raises(ValueError, match="cannot be negative"):
        project_spend({"eks_gpu_reserved": -2}, 10)


def test_every_trd_primitive_is_priced() -> None:
    for primitive in PRIMITIVE_MAP.values():
        assert project_spend({primitive: 1}, 1) > 0


# --------------------------------------------------------------------------------
# check_budget — TRD §6 table-driven acceptance gate
# --------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("projected_spend", "budget", "expected_breach", "expected_action", "expected_overage"),
    [
        # no breach
        (50.0, 100.0, False, "none", 0.0),
        (99.99, 100.0, False, "none", 0.0),
        (100.0, 100.0, False, "none", 0.0),          # boundary: equal is NOT a breach
        (0.0, 100.0, False, "none", 0.0),
        # small breach -> downgrade_tier (overage <= 20% of budget)
        (100.01, 100.0, True, "downgrade_tier", 0.01),
        (110.0, 100.0, True, "downgrade_tier", 10.0),
        (120.0, 100.0, True, "downgrade_tier", 20.0),  # boundary: exactly 20% is not >20%
        # large breach -> throttle (overage > 20% of budget)
        (120.01, 100.0, True, "throttle", 20.01),
        (150.0, 100.0, True, "throttle", 50.0),
        (500.0, 100.0, True, "throttle", 400.0),
        # a different budget scale behaves identically
        (12.0, 10.0, True, "downgrade_tier", 2.0),
        (13.0, 10.0, True, "throttle", 3.0),
    ],
)
def test_check_budget_table(
    projected_spend: float,
    budget: float,
    expected_breach: bool,
    expected_action: str,
    expected_overage: float,
) -> None:
    result = check_budget(projected_spend, budget)
    assert set(result) == BUDGET_RESULT_KEYS
    assert result["breach_projected"] is expected_breach
    assert result["action"] == expected_action
    assert result["overage"] == pytest.approx(expected_overage)


def test_overage_is_never_negative() -> None:
    """TRD §1.7: overage >= 0.0 always."""
    for spend in (0.0, 1.0, 50.0, 99.0, 100.0, 101.0, 1000.0):
        assert check_budget(spend, 100.0)["overage"] >= 0.0


def test_action_escalates_monotonically_with_spend() -> None:
    ranks = {"none": 0, "downgrade_tier": 1, "throttle": 2}
    previous = -1
    for spend in range(0, 300, 5):
        rank = ranks[check_budget(float(spend), 100.0)["action"]]
        assert rank >= previous
        previous = rank


def test_throttle_threshold_boundary_is_exclusive() -> None:
    budget = 100.0
    exactly_at = budget * (1.0 + THROTTLE_THRESHOLD)
    assert check_budget(exactly_at, budget)["action"] == "downgrade_tier"
    assert check_budget(exactly_at + 0.01, budget)["action"] == "throttle"


def test_check_budget_rejects_invalid_input() -> None:
    with pytest.raises(ValueError, match="budget must be positive"):
        check_budget(10.0, 0.0)
    with pytest.raises(ValueError, match="budget must be positive"):
        check_budget(10.0, -5.0)
    with pytest.raises(ValueError, match="projected_spend"):
        check_budget(-1.0, 100.0)


# --------------------------------------------------------------------------------
# end-to-end: the governance layer sees a composition shift the aggregate view misses
# --------------------------------------------------------------------------------

def test_composition_shift_breaches_budget_at_constant_request_rate() -> None:
    """The cost-governance claim in one test.

    Same total served throughput either side, but shifted from cheap Bedrock units to
    expensive reserved-GPU units. An aggregate-only view sees an unchanged request rate;
    the spend projection sees a breach.
    """
    minutes_remaining = 60
    budget = 60.0

    before = {"bedrock_on_demand": 10}                              # 300 req/min of capacity
    after = {"bedrock_on_demand": 4, "eks_gpu_reserved": 18}         # 300 req/min of capacity

    spend_before = project_spend(before, minutes_remaining)
    spend_after = project_spend(after, minutes_remaining)

    assert check_budget(spend_before, budget)["breach_projected"] is False
    result_after = check_budget(spend_after, budget)
    assert result_after["breach_projected"] is True
    assert result_after["action"] == "throttle"
    assert spend_after > spend_before * 3
