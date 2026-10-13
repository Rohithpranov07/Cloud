"""Calibration run — T13.2 (Addendum §11.6, Build-Instructions T13.2).

Measures the infrastructure delays the simulation does not model, then fixes the live
experiment's time scale K *before* any real run (honesty rule H1: parameters are frozen
before results are seen, never tuned after).

1. Actuation lag: how long after ``UpdateEventSourceMapping`` raises a pool's
   MaximumConcurrency from 2 to 8 does the pool really run 8 requests at once. Measured from
   the workers' own start/end timestamps (AWS clocks), repeated 5 times.
2. Router overhead: ingress_ts - driver send time (clock-skew corrected, ± ~0.5 s), the
   driver's round trip (an upper bound), and the router's own ``Duration`` from its logs.
3. Polling delay: start_ts - ingress_ts for requests that find the pool idle.

Rule: K is the largest of {1, 2, 3, 4, 6} with  p95(actuation lag) < 0.25 x 60 / K  seconds,
i.e. the controller's actuation lands within a quarter of one simulated minute.

    python -m cloud.calibrate            # live: costs well under a cent
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import re
import statistics
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from cloud import replay

logger = logging.getLogger("cloud.calibrate")

K_CANDIDATES: tuple[int, ...] = (1, 2, 3, 4, 6)
ACTUATION_POOL = "eks_gpu_reserved"
LOW_CONCURRENCY, HIGH_CONCURRENCY = 2, 8
PRELOAD_MESSAGES = 60
PRELOAD_SERVICE_S = 6.0
REPETITIONS = 5
ROUTER_PROBES = 20
ROUTER_PROBE_SERVICE_S = 2.0
ROUTER_PROBE_GAP_S = 10.0
CALIBRATION_PATH = replay.LIVE_DIR / "calibration.json"


# --------------------------------------------------------------------------------
# Pure maths (offline-testable)
# --------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (the same definition replay.lateness_summary uses)."""
    if not values:
        return math.nan
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


def active_count_at(intervals: Iterable[tuple[float, float]], t: float) -> int:
    return sum(1 for start, end in intervals if start <= t < end)


def max_concurrency(intervals: Sequence[tuple[float, float]]) -> int:
    events = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals])  # ends sort first on ties
    level = peak = 0
    for _, delta in events:
        level += delta
        peak = max(peak, level)
    return peak


def first_time_reaching(intervals: Sequence[tuple[float, float]], level: int, after: float) -> float | None:
    """Earliest time >= ``after`` at which at least ``level`` intervals overlap, else None."""
    events = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals])
    current = 0
    for t, delta in events:
        current += delta
        if delta > 0 and t >= after and current >= level:
            return t
    return None


def actuation_lag(intervals: Sequence[tuple[float, float]], t_set: float, target: int = HIGH_CONCURRENCY) -> dict[str, float | None]:
    """Seconds from the concurrency change at ``t_set`` until the pool really runs ``target`` at once.

    Also reports the lag to the first extra slot (3 at once) and the peak seen before the change,
    which must not exceed the old limit (a sanity check on the experiment itself).
    """
    before = [(s, min(e, t_set)) for s, e in intervals if s < t_set]
    reached = first_time_reaching(intervals, target, t_set)
    first_extra = first_time_reaching(intervals, LOW_CONCURRENCY + 1, t_set)
    return {
        "lag_to_target_s": None if reached is None else reached - t_set,
        "lag_to_first_extra_slot_s": None if first_extra is None else first_extra - t_set,
        "peak_before_change": float(max_concurrency(before)) if before else 0.0,
        "peak_after_change": float(max_concurrency(list(intervals))),
    }


def choose_time_scale(p95_lag_s: float, candidates: Sequence[int] = K_CANDIDATES) -> int | None:
    """Largest K with p95 lag < 0.25 * 60 / K; None if even the smallest candidate fails."""
    ok = [k for k in candidates if p95_lag_s < 0.25 * 60.0 / k]
    return max(ok) if ok else None


def clock_skew_s(samples: Sequence[tuple[float, float]]) -> float:
    """Estimate (laptop clock - AWS clock) from (local receive time, HTTP ``Date`` epoch seconds).

    ``Date`` has 1 s resolution (the true time is floor'd), so with random call phases the mean of
    ``local - date`` overstates the skew by 0.5 s; the half-latency bias is ~0.05 s and ignored.
    """
    return statistics.fmean(local - date for local, date in samples) - 0.5


_DURATION = re.compile(r"Duration: ([0-9.]+) ms")


def report_durations_ms(messages: Iterable[str]) -> list[float]:
    """Warm invocation durations from Lambda REPORT lines (cold starts and timeouts excluded)."""
    durations: list[float] = []
    for message in messages:
        if "REPORT RequestId" not in message or "Init Duration" in message or "Status: timeout" in message:
            continue
        match = _DURATION.search(message)
        if match:
            durations.append(float(match.group(1)))
    return durations


def summarise(values: Sequence[float]) -> dict[str, float]:
    return {"n": float(len(values)), "p50": percentile(values, 0.5), "p95": percentile(values, 0.95),
            "mean": statistics.fmean(values) if values else math.nan, "max": max(values) if values else math.nan}


# --------------------------------------------------------------------------------
# AWS helpers (lazy boto3 clients passed in; fakes in the tests)
# --------------------------------------------------------------------------------


def aws_epoch(response: Mapping[str, Any]) -> float:
    """The server's ``Date`` header of a boto3 response, as epoch seconds (1 s resolution)."""
    return parsedate_to_datetime(response["ResponseMetadata"]["HTTPHeaders"]["date"]).timestamp()


def measure_skew(sts: Any, n: int = 20, now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None) -> float:
    rng = rng or random.Random(0)
    samples = []
    for _ in range(n):
        response = sts.get_caller_identity()
        samples.append((now(), aws_epoch(response)))
        sleep(rng.uniform(0.05, 0.45))  # random phase against the 1 s Date resolution
    return clock_skew_s(samples)


def preload_messages(sqs: Any, queue_url: str, run_id: str, rep: int, now: Callable[[], float] = time.time) -> int:
    """Put PRELOAD_MESSAGES worker messages straight on the queue (10 per batch call)."""
    sent = 0
    for first in range(0, PRELOAD_MESSAGES, 10):
        entries = []
        for i in range(first, min(first + 10, PRELOAD_MESSAGES)):
            body = {
                "run_id": run_id, "request_id": rep * 1000 + i, "pool": ACTUATION_POOL,
                "true_archetype": "agentic_tool_using", "predicted_archetype": "agentic_tool_using",
                "service_s": PRELOAD_SERVICE_S, "ingress_ts": now(),
            }
            entries.append({"Id": str(i), "MessageBody": json.dumps(body)})
        response = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
        if response.get("Failed"):
            raise RuntimeError(f"send_message_batch failed: {response['Failed']}")
        sent += len(entries)
    return sent


def wait_until_enabled(lam: Any, mapping_uuid: str, expected: int, *, timeout_s: float = 90.0,
                       sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
    deadline = clock() + timeout_s
    while clock() < deadline:
        mapping = lam.get_event_source_mapping(UUID=mapping_uuid)
        if mapping["State"] == "Enabled" and mapping["ScalingConfig"]["MaximumConcurrency"] == expected:
            return
        sleep(2.0)
    raise TimeoutError(f"mapping {mapping_uuid} did not reach Enabled/{expected} within {timeout_s:.0f}s")


def result_intervals(items: Sequence[Mapping[str, Any]]) -> list[tuple[float, float]]:
    return [(float(i["start_ts"]), float(i["end_ts"])) for i in items]


def run_actuation_rep(
    *, rep: int, sqs: Any, lam: Any, ddb: Any, queue_url: str, mapping_uuid: str, results_table: str,
    pre_change_s: float = 25.0, now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One repetition: 60 x 6 s messages at concurrency 2, raise to 8 after ``pre_change_s``."""
    run_id = f"calib-act-{uuid.uuid4().hex[:8]}"
    wait_until_enabled(lam, mapping_uuid, LOW_CONCURRENCY)
    preload_messages(sqs, queue_url, run_id, rep, now)
    sleep(pre_change_s)  # let the pool settle at its old limit and build a backlog

    response = lam.update_event_source_mapping(UUID=mapping_uuid, ScalingConfig={"MaximumConcurrency": HIGH_CONCURRENCY})
    t_set = aws_epoch(response)  # the AWS clock when the change request arrived (1 s resolution)
    t_set_local = now()

    # Wait for all 60 results (they stop arriving once the backlog is drained).
    deadline = time.monotonic() + 240
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        items = replay.query_run(ddb, results_table, run_id)
        if len(items) >= PRELOAD_MESSAGES:
            break
        sleep(5.0)
    if len(items) < PRELOAD_MESSAGES:
        raise RuntimeError(f"rep {rep}: only {len(items)}/{PRELOAD_MESSAGES} results after 240 s")

    metrics = actuation_lag(result_intervals(items), t_set)
    # put the pool back to 2 for the next repetition
    lam.update_event_source_mapping(UUID=mapping_uuid, ScalingConfig={"MaximumConcurrency": LOW_CONCURRENCY})
    wait_until_enabled(lam, mapping_uuid, LOW_CONCURRENCY)
    return {"rep": rep, "run_id": run_id, "t_set_aws": t_set, "t_set_local": t_set_local, **metrics}


def run_router_probes(
    *, post: replay.PostFn, ddb: Any, results_table: str, skew_s: float, n: int = ROUTER_PROBES,
    gap_s: float = ROUTER_PROBE_GAP_S, now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
    template: Mapping[str, Any],
) -> dict[str, Any]:
    """n sequential requests, spaced so the pool is idle for each. Returns raw samples + run_id."""
    run_id = f"calib-rt-{uuid.uuid4().hex[:8]}"
    sends: list[dict[str, float]] = []
    for i in range(n):
        body = {**template, "run_id": run_id, "request_id": i, "service_s": ROUTER_PROBE_SERVICE_S}
        t_send = now()
        t0 = time.monotonic()
        status, error = post(body)
        rtt = time.monotonic() - t0
        if status != 202:
            raise RuntimeError(f"probe {i}: HTTP {status} {error}")
        sends.append({"request_id": float(i), "t_send_local": t_send, "rtt_s": rtt})
        sleep(gap_s)
    sleep(ROUTER_PROBE_SERVICE_S + 8)  # let the last worker finish
    items = {int(r["request_id"]): r for r in replay.query_run(ddb, results_table, run_id)}
    rows = []
    for s in sends:
        item = items.get(int(s["request_id"]))
        if item is None:
            raise RuntimeError(f"probe {int(s['request_id'])}: no result row")
        rows.append({
            **s,
            "router_overhead_s": float(item["ingress_ts"]) - s["t_send_local"] + skew_s,  # skew-corrected
            "polling_delay_s": float(item["start_ts"]) - float(item["ingress_ts"]),
        })
    return {"run_id": run_id, "rows": rows}


def read_router_durations(logs: Any, since_epoch_s: float) -> list[float]:
    """Warm router invocation durations (ms) from CloudWatch Logs since a time (read-only)."""
    messages: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"logGroupName": "/aws/lambda/semauto-router", "startTime": int(since_epoch_s * 1000),
                                  "filterPattern": "REPORT"}
        if token:
            kwargs["nextToken"] = token
        page = logs.filter_log_events(**kwargs)
        messages.extend(e["message"] for e in page["events"])
        token = page.get("nextToken")
        if not token:
            return report_durations_ms(messages)


# --------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------


def calibration_report(actuation: Sequence[Mapping[str, Any]], probes: Sequence[Mapping[str, Any]],
                       router_durations_ms: Sequence[float], skew_s: float) -> dict[str, Any]:
    lags = [float(a["lag_to_target_s"]) for a in actuation if a["lag_to_target_s"] is not None]
    never = len(actuation) - len(lags)
    p95 = percentile(lags, 0.95) if not never else math.inf  # a rep that never reached 8 fails the rule
    first_extra = [float(a["lag_to_first_extra_slot_s"]) for a in actuation if a["lag_to_first_extra_slot_s"] is not None]
    chosen = choose_time_scale(p95)
    return {
        "actuation_lag_to_8_s": {**summarise(lags), "reps_that_never_reached_8": never},
        "actuation_lag_to_first_extra_slot_s": summarise(first_extra),
        "actuation_peak_before_change": [a["peak_before_change"] for a in actuation],
        "router_overhead_s": summarise([float(r["router_overhead_s"]) for r in probes]),
        "router_rtt_s": summarise([float(r["rtt_s"]) for r in probes]),
        "router_duration_ms": summarise(list(router_durations_ms)),
        "polling_delay_s": summarise([float(r["polling_delay_s"]) for r in probes]),
        "clock_skew_estimate_s": skew_s,
        "rule": "K = largest of {1,2,3,4,6} with p95 actuation lag < 0.25 * 60 / K",
        "k_thresholds_s": {str(k): 0.25 * 60.0 / k for k in K_CANDIDATES},
        "chosen_time_scale": chosen,
    }


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="T13.2 calibration run (live; costs well under a cent)")
    parser.add_argument("--stack-name", default=replay.DEFAULT_STACK)
    parser.add_argument("--profile", default=replay.DEFAULT_PROFILE)
    parser.add_argument("--region", default=replay.DEFAULT_REGION)
    parser.add_argument("--reps", type=int, default=REPETITIONS)
    args = parser.parse_args(argv)

    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    stack = replay.resolve_stack(session.client("cloudformation"), args.stack_name)
    sqs, ddb, logs = session.client("sqs"), session.client("dynamodb"), session.client("logs")
    lam = session.client("lambda", config=Config(read_timeout=125, retries={"total_max_attempts": 1}))
    mapping_uuid = lam.list_event_source_mappings(FunctionName=f"semauto-worker-{ACTUATION_POOL}")["EventSourceMappings"][0]["UUID"]
    started = time.time()

    skew = measure_skew(session.client("sts"))
    logger.info("clock skew (laptop - AWS) ~ %.2f s (+-0.5)", skew)

    # 2 + 3: router overhead and idle polling delay (router pre-warmed first).
    post = replay.http_post(f"{stack.api_url}/classify")
    replay.warm_router(post, 4)
    template = json.loads((Path(__file__).resolve().parents[1] / "tests/cloud/fixtures/http_v2_event.json").read_text())
    probe_body = {k: v for k, v in json.loads(template["body"]).items()}
    probes = run_router_probes(post=post, ddb=ddb, results_table=stack.tables["results"], skew_s=skew, template=probe_body)

    # 1: actuation lag, repeated.
    actuation: list[dict[str, Any]] = []
    try:
        for rep in range(1, args.reps + 1):
            logger.info("actuation repetition %d/%d", rep, args.reps)
            actuation.append(run_actuation_rep(
                rep=rep, sqs=sqs, lam=lam, ddb=ddb, queue_url=stack.queue_urls[ACTUATION_POOL],
                mapping_uuid=mapping_uuid, results_table=stack.tables["results"],
            ))
    finally:  # never leave the pool raised
        lam.update_event_source_mapping(UUID=mapping_uuid, ScalingConfig={"MaximumConcurrency": LOW_CONCURRENCY})

    durations = read_router_durations(logs, started)
    report = calibration_report(actuation, probes["rows"], durations, skew)
    replay.LIVE_DIR.mkdir(parents=True, exist_ok=True)
    CALIBRATION_PATH.write_text(json.dumps({"actuation": actuation, "probes": probes, "report": report}, indent=2, default=str))
    print(json.dumps(report, indent=2))
    return 0 if report["chosen_time_scale"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
