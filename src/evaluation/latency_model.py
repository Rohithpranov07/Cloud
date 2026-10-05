"""Request-level latency model — TRD-AWS-Addendum §9.1, §9.2 and §9.4.

Turns capacity decisions into queueing latency so the PRD's primary metric (mean
response time / SLA violations) can finally be measured, which is gap G1 in the
Addendum. Every capacity unit backs ``slots_per_unit`` concurrent servers ("slots");
a request occupies a slot for its service time and queues FIFO if none is free.

Routing uses the PREDICTED archetype (what the system knows); service time is
charged by the TRUE archetype (the real work), exactly as it would be in production.

Import direction (Addendum §9.4): this module may import from ``src.config`` and
``src.trace_gen`` (for ``COMPUTE_COST_BASE``) and nothing later than
``src.evaluation``.
"""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Mapping
from typing import Literal

import numpy as np
import pandas as pd

from src.config import ARCHETYPES, LATENCY_DEFAULTS, PRIMITIVE_MAP, PRIMITIVE_UNIT_CAPACITY
from src.controller.baseline_policy import BASELINE_POOL
from src.trace_gen.generator import COMPUTE_COST_BASE

SLOTS_PER_UNIT: int = int(LATENCY_DEFAULTS["slots_per_unit"])
ARRIVAL_SEED: int = int(LATENCY_DEFAULTS["arrival_seed"])
INITIAL_UNITS_BASELINE: int = int(LATENCY_DEFAULTS["initial_units_baseline"])
INITIAL_UNITS_AWARE_PER_PRIMITIVE: int = int(LATENCY_DEFAULTS["initial_units_aware_per_primitive"])
SLA_MULTIPLIER: dict[str, float] = {a: float(m) for a, m in LATENCY_DEFAULTS["sla_multiplier"].items()}

# Derived at import time from the existing TRD §1.6 tables — never a second, hand-written
# table (Anti-Hallucination rule 3). A unit of primitive p serves PRIMITIVE_UNIT_CAPACITY[p]
# req/min with SLOTS_PER_UNIT slots, so each request occupies a slot for:
#   base_service_s[a] = slots_per_unit * 60 / PRIMITIVE_UNIT_CAPACITY[PRIMITIVE_MAP[a]]
BASE_SERVICE_S: dict[str, float] = {
    archetype: SLOTS_PER_UNIT * 60.0 / PRIMITIVE_UNIT_CAPACITY[PRIMITIVE_MAP[archetype]]
    for archetype in ARCHETYPES
}

# SLA per archetype = sla_multiplier[a] * base_service_s[a] (Addendum §9.1).
SLA_SECONDS: dict[str, float] = {a: SLA_MULTIPLIER[a] * BASE_SERVICE_S[a] for a in ARCHETYPES}

REQUIRED_SERVICE_COLUMNS: tuple[str, ...] = ("true_archetype", "compute_cost")
REQUIRED_ARRIVAL_COLUMNS: tuple[str, ...] = ("request_id", "minute")

PER_REQUEST_COLUMNS: tuple[str, ...] = (
    "request_id",
    "minute",
    "true_archetype",
    "predicted_archetype",
    "pool",
    "arrival_s",
    "service_s",
    "wait_s",
    "response_s",
    "sla_s",
    "sla_violated",
)


def service_seconds(df: pd.DataFrame) -> pd.Series:
    """Per-request service time (Addendum §9.1).

    ``service_s[i] = base_service_s[true_archetype_i] * compute_cost_i /
    COMPUTE_COST_BASE[true_archetype_i]`` — reuses the noise the trace generator already
    draws, so no new randomness is introduced.
    """
    missing = [c for c in REQUIRED_SERVICE_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(f"service_seconds requires columns {missing}")

    base = df["true_archetype"].map(BASE_SERVICE_S)
    compute_base = df["true_archetype"].map(COMPUTE_COST_BASE)
    return (base * df["compute_cost"] / compute_base).rename("service_s")


def arrival_seconds(df: pd.DataFrame, seed: int) -> pd.Series:
    """Per-request arrival time in seconds (Addendum §9.1).

    ``arrival_s[i] = minute_i * 60 + U[0, 60)``, drawn with
    ``np.random.default_rng(seed)`` IN REQUEST_ID ORDER, so the live replay (which sends
    requests in the same order) can reproduce the exact same offsets.
    """
    missing = [c for c in REQUIRED_ARRIVAL_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(f"arrival_seconds requires columns {missing}")

    rng = np.random.default_rng(seed)
    request_order = np.argsort(df["request_id"].to_numpy(), kind="stable")
    offsets = np.empty(len(df), dtype=float)
    offsets[request_order] = rng.uniform(0.0, 60.0, size=len(df))

    minutes = df["minute"].to_numpy(dtype=float)
    return pd.Series(minutes * 60.0 + offsets, index=df.index, name="arrival_s")


# Event priority at equal timestamps, so a freed slot or a capacity change at time t is
# applied before a same-instant arrival competes for it: completions, then capacity
# boundaries, then arrivals.
_EVENT_COMPLETION, _EVENT_BOUNDARY, _EVENT_ARRIVAL = 0, 1, 2


def simulate_pool(
    arrival_s: np.ndarray, service_s: np.ndarray, slots_by_minute: Mapping[int, int]
) -> pd.DataFrame:
    """Event-driven FIFO multi-slot queue for one serving pool (Addendum §9.1, §9.4).

    A request starts at ``max(arrival, earliest time a slot is free AND busy count <
    allowed(minute(t)))``. When the allowed count drops, nothing already running is
    pre-empted — the constraint only ever gates NEW starts.

    Implemented as a discrete-event simulation over one GLOBAL chronological event queue
    (arrivals, completions, and per-minute capacity boundaries), with a single FIFO
    waiting list of requests not yet admitted. This is deliberate rather than resolving
    one request's full wait before moving to the next in arrival order: a request that
    must wait can be overtaken in real time by a later-arriving one that an earlier
    capacity boundary admits first, so only a true event-order simulation keeps the
    shared busy count correct for every request at every instant.

    Complexity: O(n log n) — the event heap holds O(n + m) entries (n arrivals, up to n
    completions, m = number of distinct minutes in ``slots_by_minute``), each pushed and
    popped at most once; each waiting-list admission is O(1) (``deque.popleft``).

    Returns a DataFrame with ``start_s``, ``end_s``, ``wait_s``, ``response_s``, aligned
    index-for-index with the input arrays (NOT sorted by arrival).
    """
    n = len(arrival_s)
    if len(service_s) != n:
        raise ValueError(f"arrival_s and service_s must have equal length, got {n} and {len(service_s)}")
    if n == 0:
        return pd.DataFrame({"start_s": [], "end_s": [], "wait_s": [], "response_s": []})
    if not slots_by_minute:
        raise ValueError("slots_by_minute must not be empty")

    min_minute = min(slots_by_minute)
    max_minute = max(slots_by_minute)

    def allowed(t: float) -> int:
        minute = max(min_minute, min(int(t // 60.0), max_minute))
        return int(slots_by_minute[minute])

    order = np.argsort(arrival_s, kind="stable")
    arrivals_sorted = arrival_s[order]
    services_sorted = service_s[order]

    starts_sorted = np.empty(n, dtype=float)
    ends_sorted = np.empty(n, dtype=float)

    events: list[tuple[float, int, int]] = []
    for position in range(n):
        heapq.heappush(events, (float(arrivals_sorted[position]), _EVENT_ARRIVAL, position))
    for minute in range(min_minute + 1, max_minute + 1):
        heapq.heappush(events, (float(minute) * 60.0, _EVENT_BOUNDARY, -1))

    waiting: deque[int] = deque()
    busy = 0

    def admit(t: float) -> None:
        nonlocal busy
        cap = allowed(t)
        while waiting and busy < cap:
            position = waiting.popleft()
            end = t + float(services_sorted[position])
            starts_sorted[position] = t
            ends_sorted[position] = end
            busy += 1
            heapq.heappush(events, (end, _EVENT_COMPLETION, position))

    while events:
        t, event_type, payload = heapq.heappop(events)
        if event_type == _EVENT_COMPLETION:
            busy -= 1
            admit(t)
        elif event_type == _EVENT_BOUNDARY:
            admit(t)
        else:  # _EVENT_ARRIVAL
            waiting.append(payload)
            admit(t)

    starts = np.empty(n, dtype=float)
    ends = np.empty(n, dtype=float)
    starts[order] = starts_sorted
    ends[order] = ends_sorted

    return pd.DataFrame(
        {
            "start_s": starts,
            "end_s": ends,
            "wait_s": starts - arrival_s,
            "response_s": ends - arrival_s,
        }
    )


def capacity_timeline(
    results: pd.DataFrame, policy: Literal["baseline", "aware"], duration_minutes: int
) -> dict[str, dict[int, int]]:
    """Pool -> {minute -> units}, including the Addendum §9.1 initial capacity.

    ``results`` is the per-minute evaluation frame (``data/evaluation_results.csv``
    shape), which only covers minutes from the evaluation's ``start_minute`` onward.
    Minutes before that use the §9.1 initial capacity: 1 unit for the baseline pool, 1
    unit per primitive for the aware policy.
    """
    if policy == "baseline":
        column_by_pool = {BASELINE_POOL: "baseline_capacity"}
        initial_units = INITIAL_UNITS_BASELINE
    elif policy == "aware":
        column_by_pool = {
            primitive: f"aware_capacity_{primitive}" for primitive in PRIMITIVE_MAP.values()
        }
        initial_units = INITIAL_UNITS_AWARE_PER_PRIMITIVE
    else:
        raise ValueError(f"unknown policy: {policy!r}; expected 'baseline' or 'aware'")

    by_minute = results.set_index("minute")
    timeline: dict[str, dict[int, int]] = {}
    for pool, column in column_by_pool.items():
        decided = by_minute[column].astype(int).to_dict()
        timeline[pool] = {
            minute: decided.get(minute, initial_units) for minute in range(duration_minutes)
        }
    return timeline


def simulate_policy_latency(
    labeled: pd.DataFrame, results: pd.DataFrame, policy: Literal["baseline", "aware"], seed: int
) -> pd.DataFrame:
    """One row per request: queueing latency for ``policy`` over ``labeled`` (Addendum §9.4).

    Routes each request by its PREDICTED archetype (aware -> ``PRIMITIVE_MAP[predicted]``,
    baseline -> the single homogeneous pool) but charges service by its TRUE archetype,
    so a misclassification still costs its real service time, exactly as in production.
    """
    if policy not in ("baseline", "aware"):
        raise ValueError(f"unknown policy: {policy!r}; expected 'baseline' or 'aware'")

    duration_minutes = int(labeled["minute"].max()) + 1
    timeline = capacity_timeline(results, policy, duration_minutes)

    service = service_seconds(labeled)
    arrival = arrival_seconds(labeled, seed=seed)

    if policy == "baseline":
        pool_of_request = pd.Series(BASELINE_POOL, index=labeled.index)
    else:
        pool_of_request = labeled["predicted_archetype"].map(PRIMITIVE_MAP)

    frames: list[pd.DataFrame] = []
    for pool in sorted(set(pool_of_request)):
        mask = (pool_of_request == pool).to_numpy()
        sub = labeled.loc[mask]
        slots_by_minute = {minute: SLOTS_PER_UNIT * units for minute, units in timeline[pool].items()}
        pool_result = simulate_pool(
            arrival.to_numpy()[mask], service.to_numpy()[mask], slots_by_minute
        )
        pool_result["pool"] = pool
        pool_result["request_id"] = sub["request_id"].to_numpy()
        pool_result["minute"] = sub["minute"].to_numpy()
        pool_result["true_archetype"] = sub["true_archetype"].to_numpy()
        pool_result["predicted_archetype"] = sub["predicted_archetype"].to_numpy()
        pool_result["arrival_s"] = arrival.to_numpy()[mask]
        pool_result["service_s"] = service.to_numpy()[mask]
        frames.append(pool_result)

    per_request = pd.concat(frames, ignore_index=True)
    per_request["sla_s"] = per_request["true_archetype"].map(SLA_SECONDS)
    per_request["sla_violated"] = per_request["response_s"] > per_request["sla_s"]
    return (
        per_request[list(PER_REQUEST_COLUMNS)]
        .sort_values("request_id")
        .reset_index(drop=True)
    )


def _stats(frame: pd.DataFrame) -> dict[str, float | int]:
    """Plain-float/int summary of one slice — keeps ``summarise_latency`` JSON-safe."""
    if len(frame) == 0:
        return {
            "n": 0,
            "mean_response_s": float("nan"),
            "p50_response_s": float("nan"),
            "p95_response_s": float("nan"),
            "p99_response_s": float("nan"),
            "sla_violation_rate": float("nan"),
        }
    response = frame["response_s"].to_numpy(dtype=float)
    return {
        "n": int(len(frame)),
        "mean_response_s": float(np.mean(response)),
        "p50_response_s": float(np.percentile(response, 50)),
        "p95_response_s": float(np.percentile(response, 95)),
        "p99_response_s": float(np.percentile(response, 99)),
        "sla_violation_rate": float(frame["sla_violated"].mean()),
    }


def summarise_latency(per_request: pd.DataFrame, shift_start: int, shift_end: int) -> dict:
    """Mean/p50/p95/p99 response and SLA-violation rate — overall, per archetype, per window.

    Windows: pre = ``minute < shift_start``; during = ``[shift_start, shift_end)``;
    post = ``minute >= shift_end``. Returns plain floats and ints only (JSON-serialisable).
    """
    df = per_request.copy()
    minute = df["minute"].to_numpy()
    df["window"] = np.select(
        [minute < shift_start, minute < shift_end],
        ["pre", "during"],
        default="post",
    )

    return {
        "overall": _stats(df),
        "by_archetype": {a: _stats(df[df["true_archetype"] == a]) for a in ARCHETYPES},
        "by_window": {w: _stats(df[df["window"] == w]) for w in ("pre", "during", "post")},
    }
