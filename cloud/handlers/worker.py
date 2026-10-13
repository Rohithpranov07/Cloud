"""Worker Lambda — one emulated serving slot (Addendum §11.1, §11.4).

Stands in for a GPU serving primitive: it holds a slot for the request's service time
(scaled by TIME_SCALE) and records when the request really started and ended. No
inference happens; queueing delay in front of it is real infrastructure behaviour.

Deployed as a zip function, so it imports only the standard library and boto3 —
nothing from ``src/`` (test-enforced). One function per pool; the event-source
mapping's MaximumConcurrency (set by the controller) is the pool's slot count.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

RESULTS_TABLE: str = "semauto-results"
TTL_SECONDS: int = 7 * 24 * 60 * 60  # §11.3: expires_at = now + 7 days


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_message(body: str) -> dict[str, Any]:
    """Return the fields the result row needs from the router's §11.3 SQS message."""
    message = json.loads(body)
    if not isinstance(message, dict):
        raise ValueError("message body must be a JSON object")

    checks: dict[str, tuple[Callable[[Any], bool], str]] = {
        "run_id": (lambda v: isinstance(v, str) and bool(v), "a non-empty string"),
        "request_id": (lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0, "a non-negative integer"),
        "pool": (lambda v: isinstance(v, str) and bool(v), "a non-empty string"),
        "true_archetype": (lambda v: isinstance(v, str), "a string"),
        "predicted_archetype": (lambda v: isinstance(v, str) and bool(v), "a non-empty string"),
        "service_s": (lambda v: _is_number(v) and v > 0, "a positive number"),
        "ingress_ts": (_is_number, "a finite number"),
    }
    fields: dict[str, Any] = {}
    for name, (ok, expected) in checks.items():
        if name not in message:
            raise ValueError(f"message is missing field '{name}'")
        if not ok(message[name]):
            raise ValueError(f"message field '{name}' must be {expected}, got {message[name]!r}")
        fields[name] = message[name]
    return fields


def _num(value: float) -> dict[str, str]:
    # Fixed-point text keeps DynamoDB Number values free of exponent notation; microsecond
    # resolution is far below the timing noise being measured.
    return {"N": f"{value:.6f}"}


def process_record(
    record: Mapping[str, Any],
    ddb: Any,
    *,
    table: str,
    time_scale: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Hold one slot for service_s / time_scale seconds, then record the result row."""
    message = parse_message(record["body"])

    start_ts = clock()
    sleep(message["service_s"] / time_scale)
    end_ts = clock()

    ddb.put_item(
        TableName=table,
        Item={
            "run_id": {"S": message["run_id"]},
            "request_id": {"N": str(message["request_id"])},
            "pool": {"S": message["pool"]},
            "true_archetype": {"S": message["true_archetype"]},
            "predicted_archetype": {"S": message["predicted_archetype"]},
            "service_s": _num(message["service_s"]),
            "ingress_ts": _num(message["ingress_ts"]),
            "start_ts": _num(start_ts),
            "end_ts": _num(end_ts),
            "expires_at": {"N": str(int(end_ts) + TTL_SECONDS)},
        },
    )


def process_batch(
    event: Mapping[str, Any],
    ddb: Any,
    *,
    table: str,
    time_scale: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> dict[str, list[dict[str, str]]]:
    """Process every record (batch size is 1 by design, but loop anyway).

    A failing record is logged with its traceback and reported in ``batchItemFailures``
    so only it returns to the queue; the rest of the batch still succeeds. Response
    shape and semantics:
    https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html
    """
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = record["messageId"]  # a record without one is a malformed event: fail the batch
        try:
            process_record(record, ddb, table=table, time_scale=time_scale, sleep=sleep, clock=clock)
        except Exception:
            logger.exception("record %s failed; reporting it in batchItemFailures", message_id)
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def load_time_scale(env: Mapping[str, str]) -> float:
    time_scale = float(env.get("TIME_SCALE", "1"))
    if not time_scale > 0:
        raise ValueError(f"TIME_SCALE must be positive, got {time_scale}")
    return time_scale


@functools.cache
def _dynamodb() -> Any:
    return boto3.client("dynamodb")


def handler(event: dict, context: object) -> dict:
    """SQS batch (SQS → Lambda event, batch size 1) → ``{"batchItemFailures": [...]}``.

    Event shape: https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html
    RESULTS_TABLE defaults to the §11.3 table name; an invalid TIME_SCALE raises,
    failing the whole batch visibly.
    """
    return process_batch(
        event,
        _dynamodb(),
        table=os.environ.get("RESULTS_TABLE", RESULTS_TABLE),
        time_scale=load_time_scale(os.environ),
    )
