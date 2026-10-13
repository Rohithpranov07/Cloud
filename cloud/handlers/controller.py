"""Controller Lambda — tick → forecast → decide → actuate → govern → record (§11.4).

Invoked synchronously by the replay driver once per simulated minute. It reuses the
unchanged ``src`` forecaster, controllers and cost governance, so a live tick makes the
same decision ``run_comparison.simulate(recalibrate=False)`` makes for that minute.
It is stateless: the run's history lives in ``semauto-decisions``.

Live-run spend governance deliberately differs from ``run_comparison`` for the baseline:
each live run is one policy, so the baseline projects from its *own* incurred spend
(units × ``_blended_unit_cost`` per minute). ``run_comparison`` projects the baseline from
the aware policy's incurred spend because both policies share one simulated run. The
aware policy's projection is identical in both.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cloud.adapters import count_item_from_dynamodb, counts_to_labeled
from cloud.common import Settings, client, emit_emf, load_settings
from src.config import EVALUATION_DEFAULTS, PRIMITIVE_MAP, TRACE_DEFAULTS
from src.controller.archetype_aware_policy import archetype_aware_scaling_decision
from src.controller.baseline_policy import BASELINE_POOL, baseline_scaling_decision
from src.cost_governance.budget_projector import check_budget, project_spend
from src.evaluation.latency_model import INITIAL_UNITS_AWARE_PER_PRIMITIVE, INITIAL_UNITS_BASELINE
from src.evaluation.run_comparison import _blended_unit_cost
from src.forecaster.per_archetype_forecaster import forecast_aggregate, forecast_all_archetypes

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DECISIONS_TABLE: str = "semauto-decisions"
COUNTS_TABLE: str = "semauto-counts"
TTL_SECONDS: int = 7 * 24 * 60 * 60
EMF_NAMESPACE: str = "SemAuto"

HORIZON: int = int(EVALUATION_DEFAULTS["forecast_horizon"])
PER_UNIT_CAPACITY: int = int(EVALUATION_DEFAULTS["baseline_per_unit_capacity"])
BUDGET: float = float(EVALUATION_DEFAULTS["budget"])
DURATION_MINUTES: int = int(TRACE_DEFAULTS["duration_minutes"])
SIZING_RULES: tuple[str, ...] = ("round", "ceil")

POOLS_BY_POLICY: dict[str, tuple[str, ...]] = {
    "baseline": (BASELINE_POOL,),
    "aware": tuple(PRIMITIVE_MAP.values()),
}

# ESM MaximumConcurrency range for SQS (botocore service model: min 2, max 1000):
# https://docs.aws.amazon.com/lambda/latest/api/API_UpdateEventSourceMapping.html
ESM_MIN_CONCURRENCY: int = 2
ESM_MAX_CONCURRENCY: int = 1000


def initial_capacity(policy: str) -> dict[str, int]:
    """§9.1 capacity before the first decision: 1 baseline unit, 1 unit per primitive."""
    units = INITIAL_UNITS_BASELINE if policy == "baseline" else INITIAL_UNITS_AWARE_PER_PRIMITIVE
    return {pool: units for pool in POOLS_BY_POLICY[policy]}


def max_concurrency(units: int, slots_per_unit: int) -> int:
    """§11.4 actuation rule: MaximumConcurrency = max(2, slots_per_unit × units)."""
    value = max(ESM_MIN_CONCURRENCY, slots_per_unit * units)
    if value > ESM_MAX_CONCURRENCY:
        raise ValueError(f"MaximumConcurrency {value} exceeds the ESM limit of {ESM_MAX_CONCURRENCY}")
    return value


@dataclass(frozen=True)
class TickRequest:
    run_id: str
    policy: str
    sizing: str
    minute: int


def parse_tick(event: Mapping[str, Any]) -> TickRequest:
    run_id, policy, sizing, minute = (event.get(k) for k in ("run_id", "policy", "sizing", "minute"))
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError(f"tick needs a non-empty 'run_id', got {run_id!r}")
    if policy not in POOLS_BY_POLICY:
        raise ValueError(f"tick 'policy' must be 'baseline' or 'aware', got {policy!r}")
    if sizing not in SIZING_RULES:
        raise ValueError(f"tick 'sizing' must be 'round' or 'ceil', got {sizing!r}")
    if not isinstance(minute, int) or isinstance(minute, bool) or not 0 <= minute < DURATION_MINUTES:
        raise ValueError(f"tick 'minute' must be an integer in [0, {DURATION_MINUTES}), got {minute!r}")
    return TickRequest(run_id=run_id, policy=policy, sizing=sizing, minute=minute)


@dataclass(frozen=True)
class PreviousDecision:
    minute: int
    capacity: dict[str, int]
    actuated: dict[str, int]
    notified: bool


def _previous_from_item(item: Mapping[str, Any]) -> PreviousDecision:
    return PreviousDecision(
        minute=int(item["minute"]["N"]),
        capacity={pool: int(v["N"]) for pool, v in item["capacity"]["M"].items()},
        actuated={pool: int(v["N"]) for pool, v in item["actuated"]["M"].items()},
        notified=bool(item["notified"]["BOOL"]),
    )


def _query_all(ddb: Any, **params: Any) -> list[dict[str, Any]]:
    """Query with pagination (results are ordered by the numeric sort key)."""
    items: list[dict[str, Any]] = []
    start_key: Any = None
    while True:
        page = ddb.query(**params, **({"ExclusiveStartKey": start_key} if start_key else {}))
        items.extend(page.get("Items", []))
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            return items


def decide(
    policy: str, sizing: str, frame: Any, previous_capacity: Mapping[str, int]
) -> tuple[dict[str, int], dict[str, float]]:
    """Forecast and decide with the src functions, exactly as simulate() does."""
    if policy == "aware":
        forecast = forecast_all_archetypes(frame, HORIZON)
        capacity = dict(previous_capacity)
        for decision in archetype_aware_scaling_decision(forecast, dict(previous_capacity), sizing):
            capacity[decision["target_pool"]] = decision["new_capacity"]
        peaks = {archetype: float(series.max()) for archetype, series in forecast.items()}
        return capacity, peaks

    aggregate = forecast_aggregate(frame, HORIZON)
    new_units = baseline_scaling_decision(aggregate, previous_capacity[BASELINE_POOL], PER_UNIT_CAPACITY, sizing)[
        "new_capacity"
    ]
    return {BASELINE_POOL: int(new_units)}, {"aggregate": float(aggregate.max())}


def minute_cost(policy: str, capacity: Mapping[str, int]) -> float:
    """Spend incurred by one minute at this capacity (simulated TRD §1.6 prices)."""
    if policy == "aware":
        return project_spend(dict(capacity), 1)
    return capacity[BASELINE_POOL] * _blended_unit_cost(PER_UNIT_CAPACITY)


def projected_spend(policy: str, incurred: float, capacity: Mapping[str, int], minutes_remaining: int) -> float:
    """End-of-window projection: spend so far + this capacity held for the rest of the window."""
    if policy == "aware":
        return incurred + project_spend(dict(capacity), minutes_remaining)
    return incurred + capacity[BASELINE_POOL] * _blended_unit_cost(PER_UNIT_CAPACITY) * minutes_remaining


def _n(value: float) -> dict[str, str]:
    return {"N": f"{value:.6f}"}


def tick(
    event: Mapping[str, Any],
    ddb: Any,
    lam: Any,
    sns: Any,
    settings: Settings,
    *,
    topic_arn: str,
    now: Callable[[], float],
    emit: Callable[..., Any] = emit_emf,
) -> dict[str, Any]:
    """One control-loop step for one run and one simulated minute."""
    req = parse_tick(event)
    pools = POOLS_BY_POLICY[req.policy]

    # 1. Telemetry: every counter for this run up to and including this minute.
    #    ConsistentRead is valid on base-table queries (not on GSIs):
    #    https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_Query.html
    #    "minute" is a DynamoDB reserved word, hence the #m placeholder.
    count_items = _query_all(
        ddb,
        TableName=settings.counts_table,
        KeyConditionExpression="run_id = :r AND #m <= :m",
        ExpressionAttributeNames={"#m": "minute"},
        ExpressionAttributeValues={":r": {"S": req.run_id}, ":m": {"N": str(req.minute)}},
        ConsistentRead=True,
    )
    frame = counts_to_labeled(count_item_from_dynamodb(item) for item in count_items)

    # 2. History: every earlier decision for this run (stateless controller).
    previous: Sequence[PreviousDecision] = [
        _previous_from_item(item)
        for item in _query_all(
            ddb,
            TableName=settings.decisions_table,
            KeyConditionExpression="run_id = :r AND #m < :m",
            ExpressionAttributeNames={"#m": "minute"},
            ExpressionAttributeValues={":r": {"S": req.run_id}, ":m": {"N": str(req.minute)}},
            ConsistentRead=True,
        )
    ]
    last = previous[-1] if previous else None
    previous_capacity = last.capacity if last else initial_capacity(req.policy)
    previous_actuated = (
        last.actuated
        if last
        else {pool: max_concurrency(units, settings.slots_per_unit) for pool, units in initial_capacity(req.policy).items()}
    )

    # 3. Forecast and decide with the unchanged src functions.
    capacity, peaks = decide(req.policy, req.sizing, frame, previous_capacity)
    capacity = {pool: int(capacity.get(pool, 0)) for pool in pools}

    # 4. Actuate only the pools whose MaximumConcurrency changes.
    actuated: dict[str, int] = {}
    changes: dict[str, int] = {}
    for pool in pools:
        target = max_concurrency(capacity[pool], settings.slots_per_unit)
        if capacity[pool] == 0:
            logger.info("pool %s needs 0 units; holding its ESM at the floor of %d", pool, ESM_MIN_CONCURRENCY)
        if target != previous_actuated.get(pool):
            lam.update_event_source_mapping(
                UUID=settings.esm_uuids[pool], ScalingConfig={"MaximumConcurrency": target}
            )
            changes[pool] = target
        actuated[pool] = target

    # 5. Govern: incurred = sum of per-minute spend over every decision so far, this one included.
    incurred = sum(minute_cost(req.policy, p.capacity) for p in previous) + minute_cost(req.policy, capacity)
    projection = projected_spend(req.policy, incurred, capacity, DURATION_MINUTES - req.minute)
    budget = check_budget(projection, BUDGET)

    notified = bool(last.notified) if last else False
    if budget["breach_projected"] and not notified:
        sns.publish(
            TopicArn=topic_arn,
            Subject=f"SemAuto: budget breach projected ({req.run_id})",
            Message=json.dumps(
                {
                    "run_id": req.run_id,
                    "policy": req.policy,
                    "sizing": req.sizing,
                    "minute": req.minute,
                    "action": budget["action"],
                    "overage": budget["overage"],
                    "projected_spend": projection,
                    "budget": BUDGET,
                }
            ),
        )
        notified = True

    # 6. Record the decision (§11.3).
    tick_ts = float(now())
    item = {
        "run_id": {"S": req.run_id},
        "minute": {"N": str(req.minute)},
        "policy": {"S": req.policy},
        "sizing": {"S": req.sizing},
        "capacity": {"M": {pool: {"N": str(units)} for pool, units in capacity.items()}},
        "forecast": {"M": {key: _n(peak) for key, peak in peaks.items()}},
        "projected_spend": _n(projection),
        "budget_action": {"S": budget["action"]},
        "breach_projected": {"BOOL": bool(budget["breach_projected"])},
        "actuated": {"M": {pool: {"N": str(value)} for pool, value in actuated.items()}},
        "notified": {"BOOL": notified},
        "tick_ts": _n(tick_ts),
        "expires_at": {"N": str(int(tick_ts) + TTL_SECONDS)},
    }
    ddb.put_item(TableName=settings.decisions_table, Item=item)

    # 7. Metrics (EMF): per-pool units, per-archetype forecast peak, spend vs budget.
    for pool, units in capacity.items():
        emit(EMF_NAMESPACE, {"run_id": req.run_id, "policy": req.policy, "pool": pool},
             {"Units": units}, units={"Units": "Count"}, now=now)
    for key, peak in peaks.items():
        emit(EMF_NAMESPACE, {"run_id": req.run_id, "policy": req.policy, "archetype": key},
             {"ForecastPeak": peak}, units={"ForecastPeak": "Count"}, now=now)
    emit(EMF_NAMESPACE, {"run_id": req.run_id, "policy": req.policy},
         {"ProjectedSpend": projection, "Budget": BUDGET}, now=now)

    return {
        "run_id": req.run_id,
        "minute": req.minute,
        "capacity": capacity,
        "changes": changes,
        "projected_spend": projection,
        "budget_action": budget["action"],
        "breach_projected": bool(budget["breach_projected"]),
        "notified": notified,
        "decision_item": item,
    }


def reset(event: Mapping[str, Any], lam: Any, settings: Settings) -> dict[str, Any]:
    """``{"reset": {...}}``: set every pool's ESM back to its §9.1 initial value."""
    payload = event["reset"]
    if not isinstance(payload, Mapping) or not {"policy", "run_id"} <= set(payload):
        raise ValueError(f"reset needs {{'policy', 'run_id'}}, got {payload!r}")
    initial = {**initial_capacity("aware"), **initial_capacity("baseline")}
    values: dict[str, int] = {}
    for pool, units in initial.items():
        values[pool] = max_concurrency(units, settings.slots_per_unit)
        lam.update_event_source_mapping(UUID=settings.esm_uuids[pool], ScalingConfig={"MaximumConcurrency": values[pool]})
    logger.info("reset for %s: %s", payload["run_id"], values)
    return {"reset": values}


@functools.cache
def _settings() -> Settings:
    return load_settings()


def handler(event: dict, context: object) -> dict:
    """``{"run_id","policy","sizing","minute"}`` | ``{"warmup": true}`` | ``{"reset": {...}}``."""
    if event.get("warmup"):
        return {"warmup": "ok"}  # module import (forecaster, statsmodels, src) is the work
    settings = _settings()
    if "reset" in event:
        return reset(event, client("lambda"), settings)
    return tick(
        event,
        client("dynamodb"),
        client("lambda"),
        client("sns"),
        settings,
        topic_arn=os.environ["SNS_TOPIC_ARN"],
        now=time.time,
    )
