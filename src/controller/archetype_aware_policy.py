"""Archetype-aware capacity controller — TRD §1.5, §1.6 and §2.

Where the baseline sizes one homogeneous pool from an aggregate number, this policy
routes each archetype's forecast demand to the AWS serving primitive suited to it
(PRIMITIVE_MAP, TRD §1.6) and sizes each primitive independently against its own
throughput (PRIMITIVE_UNIT_CAPACITY). A composition change therefore moves capacity
between primitives even when the aggregate request rate never moves at all.

Constants are read from ``configs/default.yaml`` via ``src.config`` rather than being
hardcoded here (Anti-Hallucination rule 3, T4.2 requirement).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import ARCHETYPES, PRIMITIVE_MAP, PRIMITIVE_UNIT_CAPACITY

MIN_CAPACITY: int = 1  # mirrors the baseline floor: a primitive in use never drops below 1

# Rounding rule. TRD §2 fixes the BASELINE's sizing formula as
# `max(1, round(predicted_load / per_unit_capacity))`. The aware policy deliberately uses
# the SAME rule, even though `ceil` would be the more correct choice for a capacity
# controller (`round` under-provisions whenever demand sits just above a unit boundary --
# e.g. 11.2 req/min served by one 10 req/min unit).
#
# The reason is experimental hygiene, not agreement with `round`: the two policies must
# differ ONLY in whether they can see workload composition. If the aware policy used
# `ceil` while the baseline used `round`, part of any measured improvement would come
# from the rounding rule rather than from archetype awareness -- exactly the
# not-apples-to-apples confound flagged in PRD §9.
#
# CONSEQUENCE, measured and recorded rather than hidden: `round` costs the aware policy
# about five minutes of reaction time. The forecaster detects the shift at minute 67
# (+7), but eks_gpu_reserved demand must exceed 15 req/min before `round` grants a second
# unit, which happens at minute 72 (+12). Under `ceil` the same scale-up lands at minute
# 68 (+8). TRD §6 words the controller gate as "within 10 minutes of shift_start_min", so
# the TRD-faithful, confound-free configuration misses that gate by two minutes. This is
# capacity QUANTISATION, not a detection failure. Resolving it needs a human decision --
# either apply `ceil` to both policies (a deviation from the TRD §2 baseline formula), or
# relax the §6 gate to match -- so the TRD is followed as written until that call is made.


def _summarise_forecast(forecast: pd.Series | float | int, archetype: str) -> float:
    """Peak of the forecast horizon for one archetype — same sizing rule as the baseline."""
    if isinstance(forecast, pd.Series):
        if len(forecast) == 0:
            raise ValueError(f"forecast for '{archetype}' is empty; cannot size capacity")
        value = float(np.max(forecast.to_numpy(dtype=float)))
    else:
        value = float(forecast)
    if value < 0:
        raise ValueError(f"forecast for '{archetype}' is negative ({value})")
    return value


def archetype_aware_scaling_decision(
    per_archetype_forecast: dict,
    current_capacity: dict,
) -> list[dict]:
    """Size every serving primitive from its own archetype demand.

    ``per_archetype_forecast`` is the TRD §1.4 ForecastVector: archetype -> Series.
    ``current_capacity`` maps primitive name -> units currently provisioned; primitives
    absent from it are treated as having zero units.

    Returns a list of ScalingDecision dicts (TRD §1.5), one per primitive whose ``delta``
    is non-zero. A primitive whose required capacity is unchanged emits no decision.
    """
    unknown = set(per_archetype_forecast) - set(ARCHETYPES)
    if unknown:
        raise KeyError(f"forecast contains non-TRD archetypes: {sorted(unknown)}")

    unknown_primitives = set(current_capacity) - set(PRIMITIVE_MAP.values())
    if unknown_primitives:
        raise KeyError(f"current_capacity contains unknown primitives: {sorted(unknown_primitives)}")

    # Aggregate archetype demand onto primitives. PRIMITIVE_MAP is 1:1 in the Core
    # Prototype, but summing keeps this correct if two archetypes ever share a primitive.
    demand_by_primitive: dict[str, float] = {primitive: 0.0 for primitive in PRIMITIVE_MAP.values()}
    for archetype, forecast in per_archetype_forecast.items():
        demand_by_primitive[PRIMITIVE_MAP[archetype]] += _summarise_forecast(forecast, archetype)

    decisions: list[dict] = []
    for primitive, demand in demand_by_primitive.items():
        unit_capacity = PRIMITIVE_UNIT_CAPACITY[primitive]
        # A primitive with genuinely no demand scales to zero; one with any demand keeps
        # at least one unit, so a live workload is never left with nowhere to run.
        required_units = 0 if demand <= 0.0 else max(MIN_CAPACITY, round(demand / unit_capacity))
        delta = int(required_units - current_capacity.get(primitive, 0))
        if delta == 0:
            continue
        decisions.append(
            {
                "action": "scale",
                "target_pool": primitive,
                "delta": delta,
                "new_capacity": int(required_units),
            }
        )
    return decisions
