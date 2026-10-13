"""Tests for the router Lambda — T12.2 (Addendum §11.3–§11.4, §13 "Cloud unit tests").

Real boto3 clients wrapped in botocore Stubbers with exact expected parameters.
No network and no AWS credentials: every credential source is hidden (NFR-A4).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
import pytest
from botocore.stub import Stubber

from cloud.handlers import router
from src.classifier.archetype_classifier import add_proxy_features, classify, train_classifier
from src.config import ARCHETYPES, PRIMITIVE_MAP
from src.controller.baseline_policy import BASELINE_POOL
from src.evaluation.latency_model import service_seconds
from src.trace_gen.generator import _default_config, generate_trace

NOW = 1_700_000_000.25
QUEUE_URLS = {
    pool: f"https://sqs.ap-south-1.amazonaws.com/000000000000/{pool}"
    for pool in (*PRIMITIVE_MAP.values(), BASELINE_POOL)
}
FIXTURE = Path(__file__).parent / "fixtures" / "http_v2_event.json"


@pytest.fixture(autouse=True)
def no_aws_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hide every credential source so any real AWS call would fail loudly."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))


@pytest.fixture(scope="module")
def featured() -> pd.DataFrame:
    cfg = _default_config()
    frame = add_proxy_features(generate_trace(cfg), seed=cfg.seed)
    frame["service_s"] = service_seconds(frame)
    return frame


@pytest.fixture(scope="module")
def model(featured: pd.DataFrame) -> Any:
    """The same classifier cloud.build_model bakes into the image (seed 42)."""
    return train_classifier(featured, seed=_default_config().seed)


def _body(row: pd.Series, policy: str) -> dict[str, Any]:
    return {
        "run_id": f"{policy}-ceil-20261013000000",
        "policy": policy,
        "request_id": int(row["request_id"]),
        "minute": int(row["minute"]),
        "declared_output_len": float(row["declared_output_len"]),
        "has_tool_schema": int(row["has_tool_schema"]),
        "prompt_domain_score": float(row["prompt_domain_score"]),
        "service_s": float(row["service_s"]),
        "true_archetype": str(row["true_archetype"]),
    }


class Stubbed:
    """A DynamoDB and an SQS client, both stubbed, plus the EMF lines written."""

    def __init__(self) -> None:
        self.ddb = boto3.client("dynamodb", region_name="ap-south-1")
        self.sqs = boto3.client("sqs", region_name="ap-south-1")
        self.ddb_stub = Stubber(self.ddb)
        self.sqs_stub = Stubber(self.sqs)
        self.emf_lines: list[str] = []

    def expect(self, body: dict[str, Any], predicted: str, confidence: float, pool: str) -> None:
        self.ddb_stub.add_response(
            "update_item",
            {},
            {
                "TableName": "semauto-counts",
                "Key": {"run_id": {"S": body["run_id"]}, "minute": {"N": str(body["minute"])}},
                "UpdateExpression": "ADD #a :one SET expires_at = :exp",
                "ExpressionAttributeNames": {"#a": predicted},
                "ExpressionAttributeValues": {
                    ":one": {"N": "1"},
                    ":exp": {"N": str(int(NOW) + 7 * 24 * 60 * 60)},
                },
            },
        )
        message = {**body, "predicted_archetype": predicted, "confidence": confidence, "pool": pool, "ingress_ts": NOW}
        self.sqs_stub.add_response(
            "send_message",
            {"MessageId": "m-1"},
            {"QueueUrl": QUEUE_URLS[pool], "MessageBody": json.dumps(message, sort_keys=True)},
        )

    def route(self, body: Any, model: Any) -> dict[str, Any]:
        def emit(*args: Any, **kwargs: Any) -> Any:
            return router.emit_emf(*args, write=self.emf_lines.append, **kwargs)

        with self.ddb_stub, self.sqs_stub:
            response = router.route(body, model, self.ddb, self.sqs, QUEUE_URLS, lambda: NOW, emit=emit)
            self.ddb_stub.assert_no_pending_responses()
            self.sqs_stub.assert_no_pending_responses()
        return response


@pytest.fixture
def stubbed() -> Iterator[Stubbed]:
    yield Stubbed()


def _predict(model: Any, body: dict[str, Any]) -> tuple[str, float]:
    features = pd.DataFrame([{k: body[k] for k in ("declared_output_len", "has_tool_schema", "prompt_domain_score")}])
    labeled = classify(features, model)
    return str(labeled["predicted_archetype"].iloc[0]), float(labeled["confidence"].iloc[0])


# --------------------------------------------------------------------------------
# Happy path, both policies
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["aware", "baseline"])
def test_happy_path_counts_enqueues_and_returns_202(
    policy: str, featured: pd.DataFrame, model: Any, stubbed: Stubbed
) -> None:
    body = _body(featured.iloc[0], policy)
    predicted, confidence = _predict(model, body)
    pool = PRIMITIVE_MAP[predicted] if policy == "aware" else BASELINE_POOL
    stubbed.expect(body, predicted, confidence, pool)

    response = stubbed.route(body, model)

    assert response["statusCode"] == 202
    assert response["headers"] == {"content-type": "application/json"}
    assert json.loads(response["body"]) == {
        "request_id": body["request_id"],
        "predicted_archetype": predicted,
        "confidence": confidence,
        "pool": pool,
    }
    assert len(stubbed.emf_lines) == 1
    emf = json.loads(stubbed.emf_lines[0])
    assert emf["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "SemAuto"
    assert emf["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["run_id", "policy", "predicted_archetype"]]
    assert emf["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [{"Name": "Requests", "Unit": "Count"}]
    assert (emf["run_id"], emf["policy"], emf["predicted_archetype"], emf["Requests"]) == (
        body["run_id"], policy, predicted, 1.0,
    )


# --------------------------------------------------------------------------------
# 400 on a bad body — and no AWS call at all
# --------------------------------------------------------------------------------


def _mutated(featured: pd.DataFrame, **changes: Any) -> dict[str, Any]:
    body = _body(featured.iloc[0], "aware")
    for key, value in changes.items():
        if value is _DROP:
            del body[key]
        else:
            body[key] = value
    return body


_DROP = object()


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"run_id": _DROP}, "missing field 'run_id'"),
        ({"true_archetype": _DROP}, "missing field 'true_archetype'"),
        ({"policy": "random"}, "'policy'"),
        ({"request_id": "4"}, "'request_id'"),
        ({"minute": -1}, "'minute'"),
        ({"minute": True}, "'minute'"),
        ({"has_tool_schema": 2}, "'has_tool_schema'"),
        ({"has_tool_schema": True}, "'has_tool_schema'"),
        ({"prompt_domain_score": float("nan")}, "'prompt_domain_score'"),
        ({"declared_output_len": "long"}, "'declared_output_len'"),
        ({"service_s": 0}, "'service_s'"),
        ({"run_id": "  "}, "'run_id'"),
    ],
)
def test_bad_body_returns_400_without_touching_aws(
    changes: dict[str, Any], match: str, featured: pd.DataFrame, model: Any, stubbed: Stubbed
) -> None:
    response = stubbed.route(_mutated(featured, **changes), model)  # no responses queued: any call would raise
    assert response["statusCode"] == 400
    assert match in json.loads(response["body"])["error"]
    assert stubbed.emf_lines == []


def test_non_object_body_returns_400(model: Any, stubbed: Stubbed) -> None:
    response = stubbed.route(["not", "an", "object"], model)
    assert response["statusCode"] == 400


# --------------------------------------------------------------------------------
# Routing correctness for all four archetypes
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("archetype", ARCHETYPES)
def test_aware_policy_routes_each_archetype_to_its_primitive(
    archetype: str, featured: pd.DataFrame, model: Any, stubbed: Stubbed
) -> None:
    labeled = classify(featured, model)
    row = featured[labeled["predicted_archetype"] == archetype].iloc[0]
    body = _body(row, "aware")
    predicted, confidence = _predict(model, body)
    assert predicted == archetype
    stubbed.expect(body, predicted, confidence, PRIMITIVE_MAP[archetype])

    response = stubbed.route(body, model)

    assert json.loads(response["body"])["pool"] == PRIMITIVE_MAP[archetype]


# --------------------------------------------------------------------------------
# true_archetype is never read: scrambling it changes neither prediction nor pool
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["aware", "baseline"])
def test_scrambling_true_archetype_changes_neither_prediction_nor_pool(
    policy: str, featured: pd.DataFrame, model: Any
) -> None:
    original = _body(featured.iloc[0], policy)
    predicted, confidence = _predict(model, original)
    pool = PRIMITIVE_MAP[predicted] if policy == "aware" else BASELINE_POOL

    for scrambled_value in [*ARCHETYPES, "not_an_archetype", ""]:
        stubbed = Stubbed()
        body = original | {"true_archetype": scrambled_value}
        # Exact expected params: same counter key, same queue, and the scrambled value is
        # carried through untouched for evaluation.
        stubbed.expect(body, predicted, confidence, pool)

        response = json.loads(stubbed.route(body, model)["body"])

        assert (response["predicted_archetype"], response["pool"]) == (predicted, pool)
        assert response["confidence"] == confidence


# --------------------------------------------------------------------------------
# handler: thin wrapper — event parsing, lazy model and settings
# --------------------------------------------------------------------------------


def test_handler_parses_the_v2_event_and_delegates_to_route(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_route(body: Any, clf: Any, ddb: Any, sqs: Any, queue_urls: Any, now: Any, **kwargs: Any) -> dict:
        captured.update(body=body, clf=clf, queue_urls=queue_urls, kwargs=kwargs)
        return {"statusCode": 202}

    settings = router.load_settings(
        {
            "COUNTS_TABLE": "counts-x",
            "DECISIONS_TABLE": "d",
            "RESULTS_TABLE": "r",
            **{f"QUEUE_URL_{p.upper()}": u for p, u in QUEUE_URLS.items()},
            **{f"ESM_UUID_{p.upper()}": f"uuid-{p}" for p in QUEUE_URLS},
        }
    )
    monkeypatch.setattr(router, "route", fake_route)
    monkeypatch.setattr(router, "get_model", lambda: "the-model")
    monkeypatch.setattr(router, "_settings", lambda: settings)
    monkeypatch.setattr(router, "client", lambda service: f"client:{service}")

    event = json.loads(FIXTURE.read_text())
    assert router.handler(event, None) == {"statusCode": 202}
    assert captured["body"] == json.loads(event["body"])
    assert captured["clf"] == "the-model"
    assert captured["queue_urls"] == QUEUE_URLS
    assert captured["kwargs"] == {"counts_table": "counts-x"}

    encoded = event | {"body": base64.b64encode(event["body"].encode()).decode(), "isBase64Encoded": True}
    captured.clear()
    router.handler(encoded, None)
    assert captured["body"] == json.loads(event["body"])


@pytest.mark.parametrize("event", [{}, {"body": "{not json"}])
def test_handler_returns_400_for_a_missing_or_malformed_body(event: dict) -> None:
    response = router.handler(event, None)
    assert response["statusCode"] == 400


def test_model_is_loaded_once_per_container(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[Path] = []
    monkeypatch.setattr(router.joblib, "load", lambda path: loads.append(path) or "model")
    router.get_model.cache_clear()
    try:
        assert router.get_model() == router.get_model() == "model"
        assert loads == [router.MODEL_PATH]
    finally:
        router.get_model.cache_clear()
