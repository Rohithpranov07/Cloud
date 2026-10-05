"""Archetype-agnostic capacity controller — TRD §1.5 (ScalingDecision) and §2.

This is the BASELINE, modelled on STAR's original formulation: it sees only the
aggregate requests/minute forecast and scales a single homogeneous pool. It has no
notion of what kind of request is arriving, so a pure change in workload composition
at constant aggregate rate is invisible to it — which is exactly the blind spot the
archetype-aware policy is built to expose.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

BASELINE_POOL: str = "homogeneous_pool"
MIN_CAPACITY: int = 1  # TRD §6: capacity never goes below 1


def _required_units(demand: float, unit_capacity: int, sizing: str) -> int:
    """TRD §2 / Addendum §9.3 sizing rule, selectable by keyword.

    ``round`` is exactly the TRD §2 formula (default, byte-identical to Part 1).
    ``ceil`` is the capacity-correct alternative (Addendum §8.1): a pool is never
    under-provisioned for demand that sits just above a unit boundary.
    """
    if sizing == "round":
        return max(MIN_CAPACITY, round(demand / unit_capacity))
    if sizing == "ceil":
        return max(MIN_CAPACITY, math.ceil(demand / unit_capacity))
    raise ValueError(f"unknown sizing rule: {sizing!r} (expected 'round' or 'ceil')")


def _summarise_forecast(aggregate_forecast: pd.Series | float | int) -> float:
    """Reduce a forecast horizon to the single load figure the policy sizes against.

    Accepts either a Series over the horizon (the normal case, from
    ``forecast_aggregate``) or a bare number. The peak of the horizon is used rather
    than the mean: under-provisioning costs latency, over-provisioning costs money, and
    the SLA is the tighter constraint.
    """
    if isinstance(aggregate_forecast, pd.Series):
        if len(aggregate_forecast) == 0:
            raise ValueError("aggregate_forecast is empty; cannot size capacity")
        return float(np.max(aggregate_forecast.to_numpy(dtype=float)))
    return float(aggregate_forecast)


def baseline_scaling_decision(
    aggregate_forecast: pd.Series | float | int,
    current_capacity: int,
    per_unit_capacity: int = 20,
    sizing: str = "round",
) -> dict:
    """Size one homogeneous pool from the aggregate forecast alone.

    Returns a ScalingDecision exactly as shaped in TRD §1.5:
    ``{"action", "target_pool", "delta", "new_capacity"}``.

    ``sizing="round"`` (default) reproduces the TRD §2 formula byte-for-byte.
    ``sizing="ceil"`` is the Addendum §9.3 alternative.
    """
    if per_unit_capacity <= 0:
        raise ValueError(f"per_unit_capacity must be positive, got {per_unit_capacity}")
    if current_capacity < 0:
        raise ValueError(f"current_capacity cannot be negative, got {current_capacity}")

    predicted_load = _summarise_forecast(aggregate_forecast)
    if predicted_load < 0:
        raise ValueError(f"predicted load cannot be negative, got {predicted_load}")

    required_units = _required_units(predicted_load, per_unit_capacity, sizing)
    return {
        "action": "scale",
        "target_pool": BASELINE_POOL,
        "delta": int(required_units - current_capacity),
        "new_capacity": int(required_units),
    }
