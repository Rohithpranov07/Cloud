"""Tests for the replay driver — T13.1 (Addendum §11.6). Offline: no AWS, no credentials."""

from __future__ import annotations

import csv
import http.server
import io
import json
import socketserver
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import numpy as np
import pandas as pd
import pytest
from botocore.stub import Stubber

from cloud import replay
from cloud.common import POOLS
from src.evaluation.latency_model import ARRIVAL_SEED, arrival_seconds, service_seconds
from src.evaluation.run_comparison import simulate
from src.trace_gen.generator import _default_config


@pytest.fixture(autouse=True)
def no_aws_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))


@pytest.fixture(scope="module")
def full_schedule() -> pd.DataFrame:
    return replay.build_schedule(replay.make_config())


# --------------------------------------------------------------------------------
# The schedule IS the simulation's input — exactly
# --------------------------------------------------------------------------------


def test_schedule_equals_the_simulations_inputs_exactly(full_schedule: pd.DataFrame) -> None:
    """Request IDs, minutes, arrival offsets, service times (and the proxy features the live
    router classifies) equal what simulate() feeds the latency model, bit for bit."""
    _, labeled = simulate(_default_config(), sizing="round", recalibrate=False)
    expected = labeled.assign(
        service_s=service_seconds(labeled), arrival_s=arrival_seconds(labeled, seed=ARRIVAL_SEED)
    ).sort_values("request_id").reset_index(drop=True)
    actual = full_schedule.sort_values("request_id").reset_index(drop=True)

    assert len(actual) == len(expected) == 5948
    for column in ("request_id", "minute", "arrival_s", "service_s", "true_archetype",
                   "declared_output_len", "has_tool_schema", "prompt_domain_score"):
        assert np.array_equal(actual[column].to_numpy(), expected[column].to_numpy()), column


def test_schedule_is_in_send_order_and_arrivals_stay_inside_their_minute(full_schedule: pd.DataFrame) -> None:
    assert full_schedule["arrival_s"].is_monotonic_increasing
    offset = full_schedule["arrival_s"] - full_schedule["minute"] * 60.0
    assert ((offset >= 0.0) & (offset < 60.0)).all()


def test_truncating_to_fewer_minutes_leaves_arrival_offsets_untouched(full_schedule: pd.DataFrame) -> None:
    short = replay.select_minutes(full_schedule, 30)
    assert short["minute"].max() == 29
    joined = short.merge(full_schedule, on="request_id", suffixes=("", "_full"))
    assert np.array_equal(joined["arrival_s"].to_numpy(), joined["arrival_s_full"].to_numpy())


def test_the_rate_fallback_halves_the_trace_without_touching_defaults() -> None:
    assert replay.make_config().base_rate_per_min == 50.0
    half = replay.make_config(25)
    assert half.base_rate_per_min == 25.0 and half.seed == 42
    assert 0.4 < len(replay.build_schedule(half)) / 5948 < 0.6


def test_request_body_is_exactly_the_section_11_3_ingress_schema(full_schedule: pd.DataFrame) -> None:
    row = full_schedule.iloc[0].to_dict()
    body = replay.request_body(row, "aware-ceil-x", "aware")
    assert list(body) == [
        "run_id", "policy", "request_id", "minute", "declared_output_len", "has_tool_schema",
        "prompt_domain_score", "service_s", "true_archetype",
    ]
    assert body["service_s"] == row["service_s"]  # raw: the worker divides by TIME_SCALE
    json.dumps(body)  # numpy scalars would raise


# --------------------------------------------------------------------------------
# Tick plan, run id, lateness
# --------------------------------------------------------------------------------


def test_tick_plan_ticks_at_the_end_of_each_simulated_minute() -> None:
    plan = replay.tick_plan(start_minute=10, minutes=120, time_scale=1)
    assert len(plan) == 110 and plan[0] == (10, 660.0) and plan[-1] == (119, 7200.0)
    assert replay.tick_plan(10, 120, 6)[0] == (10, 110.0)


def test_run_id_format() -> None:
    from datetime import datetime, timezone

    assert replay.new_run_id("aware", "ceil", datetime(2026, 10, 13, 9, 8, 7, tzinfo=timezone.utc)) == (
        "aware-ceil-20261013090807"
    )


def test_lateness_summary_and_limit() -> None:
    stats = replay.lateness_summary([i / 100 for i in range(1, 101)])
    assert (stats["p50"], stats["p95"], stats["p99"], stats["max"]) == (0.5, 0.95, 0.99, 1.0)
    assert replay.lateness_limit_s(1) == 0.5 and replay.lateness_limit_s(4) == 0.125
    assert np.isnan(replay.lateness_summary([])["p99"])


# --------------------------------------------------------------------------------
# Runner (fake post/tick, tiny wall clock: K=600 -> one simulated minute = 0.1 s)
# --------------------------------------------------------------------------------


def _small_runner(full_schedule: pd.DataFrame, post: Any, tick: Any, **kw: Any) -> replay.Runner:
    return replay.Runner(
        replay.select_minutes(full_schedule, 5), run_id="t-run", policy="aware", time_scale=600.0,
        minutes=5, start_minute=1, post=post, tick=tick, retry_backoff_s=0.0, **kw,
    )


def test_every_request_is_sent_exactly_once_and_never_early(full_schedule: pd.DataFrame) -> None:
    sent: list[int] = []
    lock = threading.Lock()

    def post(body: dict) -> tuple[int, str]:
        with lock:
            sent.append(body["request_id"])
        return 202, ""

    runner = _small_runner(full_schedule, post, lambda m: {"StatusCode": 200})
    result = runner.run()

    expected = replay.select_minutes(full_schedule, 5)["request_id"].tolist()
    assert sorted(sent) == sorted(expected) and len(sent) == len(set(sent))
    assert [s.request_id for s in result.sends] == sorted(expected)
    assert all(s.status == 202 and s.attempts == 1 for s in result.sends)
    assert all(s.actual_s >= s.planned_s - 1e-3 for s in result.sends)  # never before its planned time
    assert not result.aborted


def test_each_tick_runs_after_all_sends_of_its_minute_and_in_order(full_schedule: pd.DataFrame) -> None:
    finished_at: dict[int, float] = {}
    tick_started_at: dict[int, float] = {}
    lock = threading.Lock()

    def post(body: dict) -> tuple[int, str]:
        time.sleep(0.01)
        with lock:
            finished_at[body["request_id"]] = time.monotonic()
        return 202, ""

    def tick(minute: int) -> dict:
        tick_started_at[minute] = time.monotonic()
        return {"StatusCode": 200, "changes": {"eks_gpu_reserved": 4}} if minute == 3 else {"StatusCode": 200}

    runner = _small_runner(full_schedule, post, tick)
    result = runner.run()
    schedule = replay.select_minutes(full_schedule, 5)

    assert [t.minute for t in result.ticks] == [1, 2, 3, 4]
    for record in result.ticks:
        ids = schedule.loc[schedule["minute"] == record.minute, "request_id"]
        assert all(finished_at[i] <= tick_started_at[record.minute] for i in ids), record.minute
        assert record.outstanding_sends == 0 and record.status == 200
        assert record.actual_s >= record.planned_s - 1e-3
    assert json.loads(result.ticks[2].changes) == {"eks_gpu_reserved": 4}


def test_a_5xx_is_retried_but_a_4xx_is_not(full_schedule: pd.DataFrame) -> None:
    calls: dict[int, int] = {}
    lock = threading.Lock()
    target_500, target_400 = (int(i) for i in replay.select_minutes(full_schedule, 2)["request_id"].iloc[[0, 1]])

    def post(body: dict) -> tuple[int, str]:
        rid = body["request_id"]
        with lock:
            calls[rid] = calls.get(rid, 0) + 1
            n = calls[rid]
        if rid == target_500 and n == 1:
            return 500, "cold start"
        if rid == target_400:
            return 400, "bad body"
        return 202, ""

    runner = replay.Runner(
        replay.select_minutes(full_schedule, 2), run_id="t", policy="aware", time_scale=600.0, minutes=2,
        start_minute=1, post=post, tick=lambda m: {"StatusCode": 200}, retry_backoff_s=0.0,
    )
    result = runner.run()
    by_id = {s.request_id: s for s in result.sends}

    assert (by_id[target_500].status, by_id[target_500].attempts, by_id[target_500].first_status) == (202, 2, 500)
    assert (by_id[target_400].status, by_id[target_400].attempts) == (400, 1)
    assert calls[target_400] == 1


def test_a_request_that_keeps_failing_gives_up_after_the_retry_budget(full_schedule: pd.DataFrame) -> None:
    runner = replay.Runner(
        replay.select_minutes(full_schedule, 1).head(3), run_id="t", policy="aware", time_scale=600.0, minutes=1,
        start_minute=5, post=lambda b: (0, "ConnectionError"), tick=lambda m: {}, max_retries=2, retry_backoff_s=0.0,
    )
    result = runner.run()
    assert [(s.status, s.attempts) for s in result.sends] == [(0, 3)] * 3


def test_a_failing_tick_is_recorded_not_fatal(full_schedule: pd.DataFrame) -> None:
    def tick(minute: int) -> dict:
        raise RuntimeError("boom")

    result = _small_runner(full_schedule, lambda b: (202, ""), tick).run()
    assert len(result.ticks) == 4 and all(t.status == 0 and "boom" in t.function_error for t in result.ticks)
    assert len(result.sends) == len(replay.select_minutes(full_schedule, 5))


def test_abort_stops_sending_and_marks_the_run_aborted(full_schedule: pd.DataFrame) -> None:
    n_sent = 0
    runner: replay.Runner

    def post(body: dict) -> tuple[int, str]:
        nonlocal n_sent
        n_sent += 1
        if n_sent == 20:
            runner.abort.set()  # what Ctrl-C does
        return 202, ""

    runner = _small_runner(full_schedule, post, lambda m: {"StatusCode": 200})
    result = runner.run()

    assert result.aborted
    assert 20 <= len(result.sends) < len(replay.select_minutes(full_schedule, 5))


# --------------------------------------------------------------------------------
# http_post against a real local server (stdlib only)
# --------------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _Handler.seen.append(body)
        status = 500 if body.get("fail") else 202
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b'{"ok": true}' if status == 202 else b"boom")

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture
def local_server() -> Iterator[str]:
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/classify"
    server.shutdown()
    server.server_close()


def test_http_post_returns_status_and_never_raises(local_server: str) -> None:
    post = replay.http_post(local_server, timeout_s=5)
    assert post({"a": 1}) == (202, "")
    assert post({"fail": True}) == (500, "boom")
    assert _Handler.seen[-2:] == [{"a": 1}, {"fail": True}]
    status, error = replay.http_post("http://127.0.0.1:9/classify", timeout_s=2)({})
    assert status == 0 and error  # connection refused -> (0, reason)


# --------------------------------------------------------------------------------
# AWS helpers (Stubber / fakes)
# --------------------------------------------------------------------------------

QUEUES = {pool: f"https://sqs.ap-south-1.amazonaws.com/000000000000/semauto-{pool}" for pool in POOLS}


def test_purge_retries_once_after_the_documented_cooldown() -> None:
    sqs = boto3.client("sqs", region_name="ap-south-1")
    stub = Stubber(sqs)
    first = next(iter(QUEUES.values()))
    stub.add_client_error("purge_queue", service_error_code="PurgeQueueInProgress", expected_params={"QueueUrl": first})
    for url in QUEUES.values():
        stub.add_response("purge_queue", {}, {"QueueUrl": url})
    sleeps: list[float] = []
    with stub:
        replay.purge_queues(sqs, QUEUES, sleep=sleeps.append)
        stub.assert_no_pending_responses()
    assert sleeps == [60]


def _backlog_response(visible: int, in_flight: int = 0) -> dict:
    return {"Attributes": {
        "ApproximateNumberOfMessages": str(visible), "ApproximateNumberOfMessagesNotVisible": str(in_flight),
        "ApproximateNumberOfMessagesDelayed": "0",
    }}


class _FakeSqs:
    """get_queue_attributes returns a scripted total backlog (spread over the first queue)."""

    def __init__(self, totals: list[int]) -> None:
        self.totals = totals
        self.calls = 0

    def get_queue_attributes(self, QueueUrl: str, AttributeNames: list[str]) -> dict:
        first = QueueUrl == next(iter(QUEUES.values()))
        if first:
            self.calls += 1
        total = self.totals[min(self.calls - 1, len(self.totals) - 1)]
        return _backlog_response(total if first else 0)


def test_drain_needs_consecutive_empty_polls() -> None:
    fake = _FakeSqs([5, 2, 0, 1, 0, 0, 0])  # a transient 1 resets the stable count
    sleeps: list[float] = []
    assert replay.wait_for_drain(fake, QUEUES, sleep=sleeps.append, stable_polls=3, clock=lambda: 0.0)
    assert fake.calls == 7


def test_drain_times_out() -> None:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    assert not replay.wait_for_drain(_FakeSqs([3]), QUEUES, timeout_s=30, poll_s=5, sleep=sleep, clock=lambda: now[0])


def test_query_run_follows_pagination_and_parses_types() -> None:
    ddb = boto3.client("dynamodb", region_name="ap-south-1")
    stub = Stubber(ddb)
    base = {"TableName": "semauto-results", "KeyConditionExpression": "run_id = :r", "ConsistentRead": True,
            "ExpressionAttributeValues": {":r": {"S": "r1"}}}
    item1 = {"run_id": {"S": "r1"}, "request_id": {"N": "7"}, "service_s": {"N": "10.766000"}}
    item2 = {"run_id": {"S": "r1"}, "request_id": {"N": "8"}, "capacity": {"M": {"p": {"N": "2"}}}, "ok": {"BOOL": True}}
    last_key = {"run_id": {"S": "r1"}, "request_id": {"N": "7"}}
    stub.add_response("query", {"Items": [item1], "LastEvaluatedKey": last_key}, base)
    stub.add_response("query", {"Items": [item2]}, {**base, "ExclusiveStartKey": last_key})
    with stub:
        rows = replay.query_run(ddb, "semauto-results", "r1")
    assert rows == [
        {"run_id": "r1", "request_id": 7, "service_s": 10.766},
        {"run_id": "r1", "request_id": 8, "capacity": '{"p": 2}', "ok": True},
    ]


def test_export_tables_writes_the_three_csvs(tmp_path: Path) -> None:
    ddb = boto3.client("dynamodb", region_name="ap-south-1")
    stub = Stubber(ddb)
    tables = {"counts": "semauto-counts", "decisions": "semauto-decisions", "results": "semauto-results"}
    for key, name in tables.items():
        stub.add_response(
            "query", {"Items": [{"run_id": {"S": "r1"}, "x": {"N": "1"}}]},
            {"TableName": name, "KeyConditionExpression": "run_id = :r", "ConsistentRead": True,
             "ExpressionAttributeValues": {":r": {"S": "r1"}}},
        )
    with stub:
        counts = replay.export_tables(ddb, tables, "r1", tmp_path)
    assert counts == {"counts": 1, "decisions": 1, "results": 1}
    for key in tables:
        rows = list(csv.DictReader((tmp_path / f"r1_{key}.csv").open()))
        assert rows == [{"run_id": "r1", "x": "1"}]


def test_resolve_stack_reads_the_outputs() -> None:
    cfn = boto3.client("cloudformation", region_name="ap-south-1")
    outputs = {
        "ApiUrl": "https://abc.execute-api.ap-south-1.amazonaws.com/", "ControllerFunctionName": "semauto-controller",
        "CountsTableName": "semauto-counts", "DecisionsTableName": "semauto-decisions", "ResultsTableName": "semauto-results",
        "BedrockOnDemandQueueUrl": "q1", "SagemakerEndpointQueueUrl": "q2", "EksGpuReservedQueueUrl": "q3",
        "EksGpuSpotQueueUrl": "q4", "HomogeneousPoolQueueUrl": "q5",
    }
    stub = Stubber(cfn)
    stub.add_response(
        "describe_stacks",
        {"Stacks": [{"StackName": "semauto-live", "CreationTime": "2026-10-13T00:00:00Z", "StackStatus": "CREATE_COMPLETE",
                     "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()]}]},
        {"StackName": "semauto-live"},
    )
    with stub:
        stack = replay.resolve_stack(cfn, "semauto-live")
    assert stack.api_url == "https://abc.execute-api.ap-south-1.amazonaws.com"
    assert stack.queue_urls["eks_gpu_reserved"] == "q3" and set(stack.queue_urls) == set(POOLS)
    assert stack.tables["results"] == "semauto-results" and stack.controller == "semauto-controller"


def test_invoke_controller_uses_request_response_and_merges_status() -> None:
    lam = boto3.client("lambda", region_name="ap-south-1")
    stub = Stubber(lam)
    stub.add_response(
        "invoke",
        {"StatusCode": 200, "Payload": io.BytesIO(b'{"changes": {"eks_gpu_reserved": 4}}')},
        {"FunctionName": "semauto-controller", "InvocationType": "RequestResponse",
         "Payload": b'{"warmup": true}'},
    )
    with stub:
        out = replay.invoke_controller(lam, "semauto-controller", {"warmup": True})
    assert out == {"changes": {"eks_gpu_reserved": 4}, "StatusCode": 200}


def test_verify_pools_reset_flags_a_pool_that_is_not_back_at_two() -> None:
    class FakeLam:
        def __init__(self, bad_pool: str | None) -> None:
            self.bad_pool = bad_pool

        def list_event_source_mappings(self, FunctionName: str) -> dict:
            pool = FunctionName.removeprefix("semauto-worker-")
            concurrency = 4 if pool == self.bad_pool else 2
            return {"EventSourceMappings": [{"State": "Enabled", "ScalingConfig": {"MaximumConcurrency": concurrency}}]}

    replay.verify_pools_reset(FakeLam(None))
    with pytest.raises(RuntimeError, match="eks_gpu_reserved"):
        replay.verify_pools_reset(FakeLam("eks_gpu_reserved"))


def test_warm_router_stops_after_a_fully_answered_wave_and_tops_up_otherwise() -> None:
    waves: list[int] = []

    def flaky(body: dict) -> tuple[int, str]:
        waves.append(1)
        return (500, "cold") if len(waves) <= 3 else (400, "empty body")

    replay.warm_router(flaky, n=6, waves=3)
    assert len(waves) == 12  # wave 1 had 5xx probes, wave 2 answered 400s: two waves of 6, then stop

    replay.warm_router(lambda b: (400, ""), n=4, waves=3)  # fully answered first time: single wave


# --------------------------------------------------------------------------------
# Summary, driver files, CLI
# --------------------------------------------------------------------------------


def _result(n: int = 4, late: float = 0.0, aborted: bool = False) -> replay.RunResult:
    sends = [
        replay.SendRecord(request_id=i, minute=0, planned_s=float(i), actual_s=float(i) + late, lateness_s=late,
                          status=202 if i else 500, rtt_s=0.05, attempts=1, first_status=202 if i else 500)
        for i in range(n)
    ]
    ticks = [replay.TickRecord(10, 660.0, 660.1, 3.0, 200, "", '{"a": 4}', 0)]
    return replay.RunResult(sends=sends, ticks=ticks, aborted=aborted, t0_epoch=1.7e9)


def test_summary_counts_losses_and_warns_on_late_sends() -> None:
    summary = replay.summarise(_result(4), time_scale=1, export_counts={"results": 2})
    assert (summary["sent"], summary["2xx"], summary["non_2xx"]) == (4, 3, 1)
    assert summary["lost"] == 1 and "WARNING" not in summary  # 3 accepted, 2 rows exported

    noisy = replay.summarise(_result(4, late=0.2), time_scale=4, export_counts=None)  # limit 0.125 s at K=4
    assert "WARNING" in noisy and "0.125" in noisy["WARNING"]


def test_driver_files_mark_an_aborted_run(tmp_path: Path) -> None:
    replay.write_driver_files(_result(3, aborted=True), "r1", tmp_path, {"run_id": "r1", "time_scale": 2})
    rows = list(csv.DictReader((tmp_path / "r1_driver.csv").open()))
    assert len(rows) == 3 and {r["run_aborted"] for r in rows} == {"True"}
    assert {"planned_s", "actual_s", "status", "rtt_s", "attempts"} <= set(rows[0])
    assert json.loads((tmp_path / "r1_meta.json").read_text()) == {"run_id": "r1", "time_scale": 2, "aborted": True}
    assert list(csv.DictReader((tmp_path / "r1_ticks.csv").open()))[0]["latency_s"] == "3.0"


def test_dry_run_prints_the_schedule_and_touches_no_aws(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*a: Any, **k: Any) -> None:
        raise AssertionError("dry run must not create an AWS session")

    monkeypatch.setattr(boto3, "Session", forbidden)
    monkeypatch.setattr(boto3, "client", forbidden)
    code = replay.main(["--policy", "aware", "--sizing", "ceil", "--time-scale", "1", "--minutes", "120", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.count("\n  t+") == 20  # exactly the first 20 sends
    assert "110 ticks" in out and "tick minute  10 at t+  660.00s" in out and "5948 requests" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["--policy", "aware", "--sizing", "ceil", "--time-scale", "0", "--minutes", "10"],
        ["--policy", "aware", "--sizing", "ceil", "--time-scale", "1", "--minutes", "121"],
        ["--policy", "aware", "--sizing", "ceil", "--time-scale", "1", "--minutes", "5", "--start-minute", "5"],
        ["--policy", "other", "--sizing", "ceil", "--time-scale", "1", "--minutes", "10"],
    ],
)
def test_bad_arguments_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        replay.parse_args(argv)
