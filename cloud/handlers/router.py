"""Router Lambda — ingress → classify → count → enqueue (Addendum §11.3–§11.4).

Receives one request from the replay driver via API Gateway (HTTP API, payload format
2.0), classifies it from the three proxy features only, increments the per-minute
counter the controller forecasts from, and enqueues it on the pool its policy routes to.
``true_archetype`` is carried through to the result row for evaluation and is never
read by any routing logic (test-enforced).
"""

from __future__ import annotations

import base64
import functools
import json
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

import joblib
import pandas as pd

from cloud.build_model import MODEL_PATH
from cloud.common import Settings, client, emit_emf, load_settings
from src.classifier.archetype_classifier import FEATURE_COLUMNS, classify
from src.config import PRIMITIVE_MAP
from src.controller.baseline_policy import BASELINE_POOL

COUNTS_TABLE: str = "semauto-counts"
TTL_SECONDS: int = 7 * 24 * 60 * 60  # §11.3: expires_at = now + 7 days
EMF_NAMESPACE: str = "SemAuto"
POLICIES: tuple[str, ...] = ("baseline", "aware")


class BadRequest(ValueError):
    """The ingress body doesn't match the Addendum §11.3 schema."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _field(body: Mapping[str, Any], name: str, ok: Callable[[Any], bool], expected: str) -> Any:
    if name not in body:
        raise BadRequest(f"missing field '{name}'")
    value = body[name]
    if not ok(value):
        raise BadRequest(f"field '{name}' must be {expected}, got {value!r}")
    return value


def validate_body(body: Any) -> dict[str, Any]:
    """Return exactly the §11.3 ingress fields, or raise ``BadRequest``."""
    if not isinstance(body, dict):
        raise BadRequest("request body must be a JSON object")
    return {
        "run_id": _field(body, "run_id", lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
        "policy": _field(body, "policy", lambda v: v in POLICIES, "'baseline' or 'aware'"),
        "request_id": _field(body, "request_id", lambda v: _is_int(v) and v >= 0, "a non-negative integer"),
        "minute": _field(body, "minute", lambda v: _is_int(v) and v >= 0, "a non-negative integer"),
        "declared_output_len": _field(
            body, "declared_output_len", lambda v: _is_number(v) and v >= 0, "a non-negative number"
        ),
        "has_tool_schema": _field(body, "has_tool_schema", lambda v: _is_int(v) and v in (0, 1), "0 or 1"),
        "prompt_domain_score": _field(body, "prompt_domain_score", _is_number, "a finite number"),
        "service_s": _field(body, "service_s", lambda v: _is_number(v) and v > 0, "a positive number"),
        "true_archetype": _field(body, "true_archetype", lambda v: isinstance(v, str), "a string"),
    }


def _response(status: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    # HTTP API payload format 2.0 response shape:
    # https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-develop-integrations-lambda.html
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


def route(
    body: Any,
    clf: Any,
    ddb: Any,
    sqs: Any,
    queue_urls: Mapping[str, str],
    now: Callable[[], float],
    *,
    counts_table: str = COUNTS_TABLE,
    emit: Callable[..., Any] = emit_emf,
) -> dict[str, Any]:
    """Validate, classify, count, enqueue. Returns the HTTP response (202 or 400)."""
    try:
        ingress = validate_body(body)
    except BadRequest as exc:
        return _response(400, {"error": str(exc)})

    # Only the three proxy features ever reach the classifier.
    features = pd.DataFrame([{name: ingress[name] for name in FEATURE_COLUMNS}], columns=FEATURE_COLUMNS)
    labeled = classify(features, clf)
    predicted = str(labeled["predicted_archetype"].iloc[0])
    confidence = float(labeled["confidence"].iloc[0])
    pool = PRIMITIVE_MAP[predicted] if ingress["policy"] == "aware" else BASELINE_POOL

    ingress_ts = float(now())
    ddb.update_item(
        TableName=counts_table,
        Key={"run_id": {"S": ingress["run_id"]}, "minute": {"N": str(ingress["minute"])}},
        # One expression may combine clauses (each keyword once); ADD creates a missing
        # Number attribute. https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.UpdateExpressions.html
        UpdateExpression="ADD #a :one SET expires_at = :exp",
        ExpressionAttributeNames={"#a": predicted},
        ExpressionAttributeValues={":one": {"N": "1"}, ":exp": {"N": str(int(ingress_ts) + TTL_SECONDS)}},
    )

    message = {
        **ingress,
        "predicted_archetype": predicted,
        "confidence": confidence,
        "pool": pool,
        "ingress_ts": ingress_ts,
    }
    sqs.send_message(QueueUrl=queue_urls[pool], MessageBody=json.dumps(message, sort_keys=True))

    emit(
        EMF_NAMESPACE,
        {"run_id": ingress["run_id"], "policy": ingress["policy"], "predicted_archetype": predicted},
        {"Requests": 1},
        units={"Requests": "Count"},
        now=now,
    )
    return _response(
        202,
        {"request_id": ingress["request_id"], "predicted_archetype": predicted, "confidence": confidence, "pool": pool},
    )


@functools.cache
def get_model() -> Any:
    """Load the build-time classifier once per container, never per request."""
    return joblib.load(MODEL_PATH)


@functools.cache
def _settings() -> Settings:
    return load_settings()


def handler(event: dict, context: object) -> dict:
    """API Gateway HTTP API (payload format 2.0) → ``route``."""
    raw = event.get("body")
    if raw is None:
        return _response(400, {"error": "request has no body"})
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _response(400, {"error": f"body is not valid JSON: {exc.msg}"})

    settings = _settings()
    return route(
        body,
        get_model(),
        client("dynamodb"),
        client("sqs"),
        settings.queue_urls,
        time.time,
        counts_table=settings.counts_table,
    )
