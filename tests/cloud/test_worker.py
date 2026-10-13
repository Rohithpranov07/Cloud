"""Tests for the worker Lambda — T12.3 (Addendum §11.1, §11.4).

Real boto3 DynamoDB client wrapped in a Stubber; sleep and clock are injected, so no
test actually waits. No network and no AWS credentials (NFR-A4).
"""

from __future__ import annotations

import ast
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from cloud.handlers import worker

WORKER_SOURCE = Path(worker.__file__)
INGRESS_TS = 1_700_000_000.5
START_TS = 1_700_000_001.25
END_TS = 1_700_000_016.75


@pytest.fixture(autouse=True)
def no_aws_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))


def _message(**overrides: Any) -> dict[str, Any]:
    """A router SQS message (§11.3): ingress body + routing fields + ingress_ts."""
    message = {
        "run_id": "aware-ceil-20261013000000",
        "policy": "aware",
        "request_id": 4,
        "minute": 0,
        "declared_output_len": 215.875,
        "has_tool_schema": 1,
        "prompt_domain_score": 0.395,
        "service_s": 15.5,
        "true_archetype": "agentic_tool_using",
        "predicted_archetype": "agentic_tool_using",
        "confidence": 1.0,
        "pool": "eks_gpu_reserved",
        "ingress_ts": INGRESS_TS,
    }
    message.update(overrides)
    return message


def _record(message_id: str, body: Any) -> dict[str, Any]:
    return {
        "messageId": message_id,
        "receiptHandle": "AQEB-example",
        "body": body if isinstance(body, str) else json.dumps(body),
        "attributes": {"ApproximateReceiveCount": "1"},
        "messageAttributes": {},
        "md5OfBody": "unused",
        "eventSource": "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:ap-south-1:000000000000:eks_gpu_reserved",
        "awsRegion": "ap-south-1",
    }


def _expected_item(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": {"S": message["run_id"]},
        "request_id": {"N": str(message["request_id"])},
        "pool": {"S": message["pool"]},
        "true_archetype": {"S": message["true_archetype"]},
        "predicted_archetype": {"S": message["predicted_archetype"]},
        "service_s": {"N": f"{message['service_s']:.6f}"},
        "ingress_ts": {"N": f"{message['ingress_ts']:.6f}"},
        "start_ts": {"N": f"{START_TS:.6f}"},
        "end_ts": {"N": f"{END_TS:.6f}"},
        "expires_at": {"N": str(int(END_TS) + 7 * 24 * 60 * 60)},
    }


class Harness:
    def __init__(self) -> None:
        self.ddb = boto3.client("dynamodb", region_name="ap-south-1")
        self.stub = Stubber(self.ddb)
        self.sleeps: list[float] = []
        self._ticks: list[float] = []

    def expect_put(self, message: dict[str, Any]) -> None:
        self.stub.add_response("put_item", {}, {"TableName": "semauto-results", "Item": _expected_item(message)})
        self._ticks += [START_TS, END_TS]

    def run(self, records: list[dict[str, Any]], time_scale: float = 1.0) -> dict[str, Any]:
        ticks = iter(self._ticks)
        with self.stub:
            result = worker.process_batch(
                {"Records": records},
                self.ddb,
                table="semauto-results",
                time_scale=time_scale,
                sleep=self.sleeps.append,
                clock=lambda: next(ticks),
            )
            self.stub.assert_no_pending_responses()
        return result


@pytest.fixture
def harness() -> Iterator[Harness]:
    yield Harness()


# --------------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------------


def test_happy_path_holds_the_slot_and_records_the_result(harness: Harness) -> None:
    message = _message()
    harness.expect_put(message)  # exact Item: §11.3 fields + start/end + expires_at

    result = harness.run([_record("m-1", message)])

    assert result == {"batchItemFailures": []}
    assert harness.sleeps == [15.5]


@pytest.mark.parametrize("time_scale", [1.0, 2.0, 4.0, 6.0])
def test_injected_sleep_receives_service_s_divided_by_time_scale(harness: Harness, time_scale: float) -> None:
    message = _message(service_s=12.0)
    harness.expect_put(message)

    harness.run([_record("m-1", message)], time_scale=time_scale)

    assert harness.sleeps == [pytest.approx(12.0 / time_scale)]


# --------------------------------------------------------------------------------
# Failures are per-record, reported, and logged — never swallowed
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        json.dumps(["a", "list"]),
        json.dumps({k: v for k, v in _message().items() if k != "service_s"}),
        json.dumps(_message(service_s=0)),
        json.dumps(_message(request_id="4")),
        json.dumps(_message(ingress_ts=None)),
    ],
    ids=["not-json", "not-an-object", "missing-service_s", "zero-service_s", "string-request_id", "null-ingress_ts"],
)
def test_malformed_body_is_reported_in_batch_item_failures(
    harness: Harness, body: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger=worker.logger.name):
        result = harness.run([_record("bad-1", body)])  # no put_item queued: none may happen

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad-1"}]}
    assert harness.sleeps == []
    assert "bad-1" in caplog.text  # logged, not silently dropped


def test_only_the_failing_record_is_reported_in_a_mixed_batch(harness: Harness) -> None:
    good = _message(request_id=10)
    harness.expect_put(good)

    result = harness.run([_record("bad-1", "{not json"), _record("good-1", good)])

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad-1"}]}


def test_a_dynamodb_error_is_reported_not_raised(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    harness.stub.add_client_error("put_item", service_error_code="ProvisionedThroughputExceededException")
    harness._ticks += [START_TS, END_TS]

    with caplog.at_level(logging.ERROR, logger=worker.logger.name):
        result = harness.run([_record("m-1", _message())])

    assert result == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}
    assert "ProvisionedThroughputExceededException" in caplog.text


def test_a_record_without_a_message_id_fails_the_whole_batch(harness: Harness) -> None:
    record = _record("m-1", _message())
    del record["messageId"]
    with pytest.raises(KeyError):
        harness.run([record])


def test_an_empty_batch_is_a_success(harness: Harness) -> None:
    assert harness.run([]) == {"batchItemFailures": []}


# --------------------------------------------------------------------------------
# handler and configuration
# --------------------------------------------------------------------------------


def test_handler_reads_table_and_time_scale_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_process_batch(event: Any, ddb: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"batchItemFailures": []}

    monkeypatch.setattr(worker, "process_batch", fake_process_batch)
    monkeypatch.setattr(worker, "_dynamodb", lambda: "ddb")
    monkeypatch.setenv("RESULTS_TABLE", "results-x")
    monkeypatch.setenv("TIME_SCALE", "3")

    assert worker.handler({"Records": []}, None) == {"batchItemFailures": []}
    assert captured == {"table": "results-x", "time_scale": 3.0}


@pytest.mark.parametrize("value", ["0", "-1", "nan"])
def test_invalid_time_scale_raises(value: str) -> None:
    with pytest.raises(ValueError, match="TIME_SCALE"):
        worker.load_time_scale({"TIME_SCALE": value})


# --------------------------------------------------------------------------------
# Zip-function purity: standard library + boto3 only, nothing from src/ (or cloud/)
# --------------------------------------------------------------------------------


def test_worker_imports_only_the_standard_library_and_boto3() -> None:
    tree = ast.parse(WORKER_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    third_party = {name for name in imported if name not in sys.stdlib_module_names}
    assert third_party == {"boto3"}, f"worker imports non-stdlib modules: {sorted(third_party)}"
