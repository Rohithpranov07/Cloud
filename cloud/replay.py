"""Replay driver — TRD-AWS-Addendum §11.6.

Replays the seed-42 trace against the live stack in (scaled) real time and ticks the
controller once per simulated minute. The schedule is built ONLY from the simulation's own
functions and seeds, so live and simulated runs consume identical inputs:

    generate_trace -> add_proxy_features(seed=cfg.seed) -> service_seconds -> arrival_seconds

``time_scale`` K divides arrival times and service times alike, so the offered load in slots
(arrival rate x service time) is the same for every K — K only shortens the wall clock.

A tick for minute m runs at the END of simulated minute m, (m+1) * 60 / K seconds into the
run, once every send of minute m has finished, because ``simulate()`` decides for minute m
from all requests with ``minute <= m``.

    python -m cloud.replay --policy aware --sizing ceil --time-scale 1 --minutes 120 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from cloud.common import POOLS
from src.classifier.archetype_classifier import add_proxy_features
from src.config import DATA_DIR, EVALUATION_DEFAULTS
from src.evaluation.latency_model import ARRIVAL_SEED, arrival_seconds, service_seconds
from src.trace_gen.generator import TraceConfig, _default_config, generate_trace

logger = logging.getLogger("cloud.replay")

LIVE_DIR: Path = DATA_DIR / "live"
DEFAULT_STACK = "semauto-live"
DEFAULT_REGION = "ap-south-1"
DEFAULT_PROFILE = "semauto"
SEND_WORKERS = 16  # §11.6
HTTP_TIMEOUT_S = 35.0  # API Gateway's integration cap is 30 s
LATENESS_P99_LIMIT_S = 0.5  # warn when p99 lateness exceeds this / K  (T13.1)
TICK_WAIT_CAP_S = 30.0  # never let one stuck send stall the controller forever
DRAIN_TIMEOUT_S = 15 * 60  # §11.6
PURGE_COOLDOWN_S = 60  # SQS docs: deletion takes up to 60 s; same-queue purge cooldown 60 s
TABLES = ("counts", "decisions", "results")

SCHEDULE_COLUMNS = [
    "request_id", "minute", "arrival_s", "service_s", "true_archetype",
    "declared_output_len", "has_tool_schema", "prompt_domain_score",
]


# --------------------------------------------------------------------------------
# Schedule (pure, offline)
# --------------------------------------------------------------------------------


def make_config(base_rate_per_min: float | None = None) -> TraceConfig:
    """The default trace config, optionally with the Phase 13 rate fallback (T11.1)."""
    cfg = _default_config()
    return cfg if base_rate_per_min is None else replace(cfg, base_rate_per_min=float(base_rate_per_min))


def build_schedule(cfg: TraceConfig) -> pd.DataFrame:
    """Every request of the trace with its arrival offset and service time, in send order.

    Arrival offsets are drawn over the WHOLE trace in request_id order (§9.1), so truncating
    to fewer minutes afterwards (``select_minutes``) leaves them untouched.
    """
    featured = add_proxy_features(generate_trace(cfg), seed=cfg.seed)
    schedule = featured.copy()
    schedule["service_s"] = service_seconds(featured)
    schedule["arrival_s"] = arrival_seconds(featured, seed=ARRIVAL_SEED)
    return schedule[SCHEDULE_COLUMNS].sort_values(["arrival_s", "request_id"]).reset_index(drop=True)


def select_minutes(schedule: pd.DataFrame, minutes: int) -> pd.DataFrame:
    return schedule[schedule["minute"] < minutes].reset_index(drop=True)


def request_body(row: Mapping[str, Any], run_id: str, policy: str) -> dict[str, Any]:
    """The §11.3 ingress body for one scheduled request."""
    return {
        "run_id": run_id,
        "policy": policy,
        "request_id": int(row["request_id"]),
        "minute": int(row["minute"]),
        "declared_output_len": float(row["declared_output_len"]),
        "has_tool_schema": int(row["has_tool_schema"]),
        "prompt_domain_score": float(row["prompt_domain_score"]),
        "service_s": float(row["service_s"]),
        "true_archetype": str(row["true_archetype"]),
    }


def tick_plan(start_minute: int, minutes: int, time_scale: float) -> list[tuple[int, float]]:
    """(minute, due offset in wall seconds): minute m ticks as simulated minute m ends."""
    return [(m, (m + 1) * 60.0 / time_scale) for m in range(start_minute, minutes)]


def new_run_id(policy: str, sizing: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    return f"{policy}-{sizing}-{stamp}"


def lateness_summary(lateness: Sequence[float]) -> dict[str, float]:
    if not lateness:
        return {"n": 0, "p50": math.nan, "p95": math.nan, "p99": math.nan, "max": math.nan}
    ordered = sorted(lateness)

    def pct(q: float) -> float:
        return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]

    return {"n": len(ordered), "p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99), "max": ordered[-1]}


def lateness_limit_s(time_scale: float) -> float:
    return LATENESS_P99_LIMIT_S / time_scale


# --------------------------------------------------------------------------------
# Sending and ticking (injectable, offline-testable)
# --------------------------------------------------------------------------------


@dataclass
class SendRecord:
    request_id: int
    minute: int
    planned_s: float
    actual_s: float
    lateness_s: float
    status: int  # final HTTP status; 0 = no response
    rtt_s: float  # round trip of the final attempt
    attempts: int
    first_status: int
    error: str = ""


@dataclass
class TickRecord:
    minute: int
    planned_s: float
    actual_s: float
    latency_s: float
    status: int  # Lambda invoke StatusCode, 0 = the call itself failed
    function_error: str
    changes: str  # JSON of {pool: MaximumConcurrency}
    outstanding_sends: int  # sends of this minute still unfinished when the tick ran


PostFn = Callable[[dict[str, Any]], tuple[int, str]]
TickFn = Callable[[int], dict[str, Any]]


def http_post(url: str, timeout_s: float = HTTP_TIMEOUT_S) -> PostFn:
    """POST a JSON body with stdlib urllib; returns (status, error text). Never raises."""

    def post(body: dict[str, Any]) -> tuple[int, str]:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 (https only)
                response.read()
                return int(response.status), ""
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", "replace")[:200]
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 0, f"{type(exc).__name__}: {exc}"

    return post


@dataclass
class RunResult:
    sends: list[SendRecord]
    ticks: list[TickRecord]
    aborted: bool
    t0_epoch: float


class Runner:
    """Sends the schedule on a monotonic clock and ticks the controller per minute."""

    def __init__(
        self,
        schedule: pd.DataFrame,
        *,
        run_id: str,
        policy: str,
        time_scale: float,
        minutes: int,
        start_minute: int,
        post: PostFn,
        tick: TickFn,
        workers: int = SEND_WORKERS,
        max_retries: int = 2,
        retry_backoff_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        tick_wait_cap_s: float = TICK_WAIT_CAP_S,
    ) -> None:
        self.schedule = schedule
        self.run_id, self.policy = run_id, policy
        self.time_scale, self.minutes, self.start_minute = time_scale, minutes, start_minute
        self.post, self.tick = post, tick
        self.workers, self.max_retries, self.retry_backoff_s = workers, max_retries, retry_backoff_s
        self.clock, self.sleep, self.tick_wait_cap_s = clock, sleep, tick_wait_cap_s

        self.abort = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict[int, int] = {
            int(m): int(n) for m, n in schedule.groupby("minute").size().items()
        }
        self._minute_done = {m: threading.Event() for m in range(minutes)}
        for minute in range(minutes):
            if self._pending.get(minute, 0) == 0:
                self._minute_done[minute].set()
        self._t0 = 0.0

    # -- helpers ---------------------------------------------------------------

    def _wait_until(self, deadline: float) -> bool:
        """Sleep until ``deadline`` on the run clock; False if aborted first."""
        while not self.abort.is_set():
            remaining = deadline - self.clock()
            if remaining <= 0:
                return True
            self.sleep(min(remaining, 0.05))
        return False

    def _finish_minute(self, minute: int) -> None:
        with self._lock:
            self._pending[minute] -= 1
            if self._pending[minute] == 0:
                self._minute_done[minute].set()

    # -- one request -----------------------------------------------------------

    def _send_one(self, row: Mapping[str, Any]) -> SendRecord:
        minute = int(row["minute"])
        planned = float(row["arrival_s"]) / self.time_scale
        body = request_body(row, self.run_id, self.policy)
        started = self.clock() - self._t0
        status, error, first_status, attempts, rtt = 0, "", 0, 0, 0.0
        try:
            for attempt in range(1, self.max_retries + 2):
                attempts = attempt
                t_attempt = self.clock()
                status, error = self.post(body)
                rtt = self.clock() - t_attempt
                if attempt == 1:
                    first_status = status
                # 2xx is done; a 4xx is our bug and retrying cannot fix it; 5xx/0 is worth a retry.
                if 200 <= status < 300 or 400 <= status < 500 or self.abort.is_set():
                    break
                if attempt <= self.max_retries:
                    self.sleep(self.retry_backoff_s * attempt)
        finally:
            self._finish_minute(minute)
        return SendRecord(
            request_id=int(row["request_id"]), minute=minute, planned_s=planned, actual_s=started,
            lateness_s=max(0.0, started - planned), status=status, rtt_s=rtt, attempts=attempts,
            first_status=first_status, error=error,
        )

    # -- the controller ticker -------------------------------------------------

    def _ticker(self, ticks: list[TickRecord]) -> None:
        for minute, due in tick_plan(self.start_minute, self.minutes, self.time_scale):
            if not self._wait_until(self._t0 + due):
                return
            # Wait for this minute's sends (their counters must be written before the controller
            # reads them), but never forever.
            give_up = self.clock() + self.tick_wait_cap_s
            while not self._minute_done[minute].is_set() and self.clock() < give_up and not self.abort.is_set():
                self.sleep(0.02)
            if self.abort.is_set():
                return
            with self._lock:
                outstanding = self._pending.get(minute, 0)
            if outstanding:
                logger.warning("tick %d running with %d sends of that minute still unfinished", minute, outstanding)
            started = self.clock()
            try:
                result = self.tick(minute)
                status = int(result.get("StatusCode", 0))
                function_error = str(result.get("FunctionError", ""))
                changes = json.dumps(result.get("changes", {}), sort_keys=True)
            except Exception as exc:  # the tick must never kill the run; the failure is recorded loudly
                logger.exception("tick %d failed", minute)
                status, function_error, changes = 0, f"{type(exc).__name__}: {exc}", "{}"
            ticks.append(TickRecord(
                minute=minute, planned_s=due, actual_s=started - self._t0, latency_s=self.clock() - started,
                status=status, function_error=function_error, changes=changes, outstanding_sends=outstanding,
            ))

    # -- the run ---------------------------------------------------------------

    def run(self) -> RunResult:
        ticks: list[TickRecord] = []
        futures: list[Future[SendRecord]] = []
        self._t0 = self.clock()
        t0_epoch = time.time()
        ticker = threading.Thread(target=self._ticker, args=(ticks,), name="ticker", daemon=True)
        ticker.start()
        executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="send")
        try:
            for row in self.schedule.to_dict("records"):
                if not self._wait_until(self._t0 + float(row["arrival_s"]) / self.time_scale):
                    break
                futures.append(executor.submit(self._send_one, row))
            ticker.join()
        except KeyboardInterrupt:
            logger.error("interrupted: stopping sends; the run will still drain and export")
            self.abort.set()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            ticker.join(timeout=5)
        sends = [f.result() for f in futures if f.done() and not f.cancelled()]
        sends.sort(key=lambda s: s.request_id)
        return RunResult(sends=sends, ticks=ticks, aborted=self.abort.is_set(), t0_epoch=t0_epoch)


# --------------------------------------------------------------------------------
# AWS access (lazy boto3; never imported by the offline tests' pure paths)
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stack:
    api_url: str
    controller: str
    queue_urls: dict[str, str]
    tables: dict[str, str]


def resolve_stack(cfn: Any, stack_name: str) -> Stack:
    """Read the deployed stack's outputs (read-only)."""
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["Outputs"]
    }
    queue_keys = {
        "bedrock_on_demand": "BedrockOnDemandQueueUrl", "sagemaker_endpoint": "SagemakerEndpointQueueUrl",
        "eks_gpu_reserved": "EksGpuReservedQueueUrl", "eks_gpu_spot": "EksGpuSpotQueueUrl",
        "homogeneous_pool": "HomogeneousPoolQueueUrl",
    }
    return Stack(
        api_url=outputs["ApiUrl"].rstrip("/"),
        controller=outputs["ControllerFunctionName"],
        queue_urls={pool: outputs[key] for pool, key in queue_keys.items()},
        tables={
            "counts": outputs["CountsTableName"],
            "decisions": outputs["DecisionsTableName"],
            "results": outputs["ResultsTableName"],
        },
    )


def invoke_controller(lam: Any, function_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Synchronous (RequestResponse) invoke; returns the function's JSON plus StatusCode/FunctionError."""
    response = lam.invoke(
        FunctionName=function_name, InvocationType="RequestResponse", Payload=json.dumps(payload).encode()
    )
    body = json.loads(response["Payload"].read() or b"{}")
    out: dict[str, Any] = dict(body) if isinstance(body, dict) else {"response": body}
    out["StatusCode"] = response["StatusCode"]
    if "FunctionError" in response:
        out["FunctionError"] = response["FunctionError"]
    return out


def purge_queues(sqs: Any, queue_urls: Mapping[str, str], sleep: Callable[[float], None] = time.sleep) -> None:
    """PurgeQueue on every queue, respecting the 60 s same-queue cooldown (SQS docs)."""
    for pool, url in queue_urls.items():
        try:
            sqs.purge_queue(QueueUrl=url)
        except sqs.exceptions.PurgeQueueInProgress:
            logger.warning("%s: purged within the last 60 s; waiting %d s then retrying once", pool, PURGE_COOLDOWN_S)
            sleep(PURGE_COOLDOWN_S)
            sqs.purge_queue(QueueUrl=url)


def queue_backlog(sqs: Any, queue_urls: Mapping[str, str]) -> dict[str, int]:
    """visible + in flight + delayed messages per queue."""
    backlog: dict[str, int] = {}
    for pool, url in queue_urls.items():
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            ],
        )["Attributes"]
        backlog[pool] = sum(int(v) for v in attrs.values())
    return backlog


def wait_for_drain(
    sqs: Any, queue_urls: Mapping[str, str], *, timeout_s: float = DRAIN_TIMEOUT_S, poll_s: float = 5.0,
    stable_polls: int = 3, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> bool:
    """True once every queue reads empty for ``stable_polls`` consecutive polls (the counts are
    approximate); False on timeout.
    """
    deadline = clock() + timeout_s
    stable = 0
    while clock() < deadline:
        backlog = queue_backlog(sqs, queue_urls)
        stable = stable + 1 if not any(backlog.values()) else 0
        if stable >= stable_polls:
            return True
        logger.info("draining: %s", {p: n for p, n in backlog.items() if n})
        sleep(poll_s)
    return False


def _plain(value: Any) -> Any:
    """DynamoDB attribute value -> a CSV-friendly Python value (maps become JSON text)."""
    (kind, inner), = value.items()
    if kind == "N":
        number = float(inner)
        return int(number) if number.is_integer() and "." not in inner and "e" not in inner.lower() else number
    if kind in ("S", "BOOL"):
        return inner
    if kind == "M":
        return json.dumps({k: _plain(v) for k, v in inner.items()}, sort_keys=True)
    if kind == "L":
        return json.dumps([_plain(v) for v in inner])
    raise ValueError(f"unsupported DynamoDB attribute type {kind}")


def query_run(ddb: Any, table: str, run_id: str) -> list[dict[str, Any]]:
    """Every item for a run_id, following LastEvaluatedKey pagination."""
    items: list[dict[str, Any]] = []
    start_key: Any = None
    while True:
        page = ddb.query(
            TableName=table, KeyConditionExpression="run_id = :r", ConsistentRead=True,
            ExpressionAttributeValues={":r": {"S": run_id}},
            **({"ExclusiveStartKey": start_key} if start_key else {}),
        )
        items.extend({k: _plain(v) for k, v in item.items()} for item in page["Items"])
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            return items


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str] | None = None) -> None:
    columns = list(fieldnames) if fieldnames else sorted({k for row in rows for k in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export_tables(ddb: Any, tables: Mapping[str, str], run_id: str, out_dir: Path) -> dict[str, int]:
    """Write ``{run_id}_{counts|decisions|results}.csv``; returns row counts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for key in TABLES:
        rows = query_run(ddb, tables[key], run_id)
        write_csv(out_dir / f"{run_id}_{key}.csv", rows)
        counts[key] = len(rows)
    return counts


def write_driver_files(result: RunResult, run_id: str, out_dir: Path, meta: Mapping[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    aborted = result.aborted
    write_csv(
        out_dir / f"{run_id}_driver.csv",
        [{**asdict(s), "run_aborted": aborted} for s in result.sends],
        [*(f for f in SendRecord.__dataclass_fields__), "run_aborted"],
    )
    write_csv(out_dir / f"{run_id}_ticks.csv", [asdict(t) for t in result.ticks], list(TickRecord.__dataclass_fields__))
    (out_dir / f"{run_id}_meta.json").write_text(json.dumps({**meta, "aborted": aborted}, indent=2, sort_keys=True))


def summarise(result: RunResult, time_scale: float, export_counts: Mapping[str, int] | None) -> dict[str, Any]:
    sends = result.sends
    ok = [s for s in sends if 200 <= s.status < 300]
    summary: dict[str, Any] = {
        "sent": len(sends), "2xx": len(ok), "non_2xx": len(sends) - len(ok),
        "retried": sum(1 for s in sends if s.attempts > 1), "ticks": len(result.ticks),
        "tick_failures": sum(1 for t in result.ticks if t.status != 200 or t.function_error),
        "lateness_s": lateness_summary([s.lateness_s for s in sends]), "aborted": result.aborted,
    }
    if export_counts is not None:
        summary["export_rows"] = dict(export_counts)
        summary["lost"] = len(ok) - export_counts.get("results", 0)
    late = summary["lateness_s"]["p99"]
    if sends and late > lateness_limit_s(time_scale):
        summary["WARNING"] = (
            f"p99 send lateness {late:.3f}s exceeds {lateness_limit_s(time_scale):.3f}s "
            f"(0.5 s / K): timing fidelity is degraded; raise --workers or lower K"
        )
    return summary


# --------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------


def warm_router(post: PostFn, n: int, waves: int = 3) -> None:
    """Cold-start ``n`` router containers with concurrent empty-body POSTs (400 = warmed).

    A cold router needs ~17-21 s, longer than its 10 s timeout (T12.6), so the first wave can
    partly 5xx; further waves top it up. 400/202 both mean the container is initialised.
    """
    for wave in range(1, waves + 1):
        with ThreadPoolExecutor(max_workers=n) as pool:
            statuses = [s for s, _ in pool.map(lambda _i: post({}), range(n))]
        warm = sum(1 for s in statuses if s in (202, 400))
        logger.info("router warm-up wave %d: %d/%d answered (%s)", wave, warm, n, sorted(statuses))
        if warm == n:
            return
    logger.warning("router warm-up: not every probe answered; retries will absorb remaining cold starts")


def verify_pools_reset(lam: Any, expected: int = 2) -> None:
    """After reset every mapping must be Enabled at MaximumConcurrency == expected."""
    for pool in POOLS:
        mapping = lam.list_event_source_mappings(FunctionName=f"semauto-worker-{pool}")["EventSourceMappings"][0]
        if mapping["State"] != "Enabled" or mapping["ScalingConfig"]["MaximumConcurrency"] != expected:
            raise RuntimeError(f"pool {pool} not reset: {mapping['State']}, {mapping['ScalingConfig']}")


def dry_run(schedule: pd.DataFrame, run_id: str, args: argparse.Namespace, cfg: TraceConfig) -> None:
    k = args.time_scale
    plan = tick_plan(args.start_minute, args.minutes, k)
    wall_min = args.minutes / k
    print(f"DRY RUN (no AWS calls)  run_id={run_id}  policy={args.policy} sizing={args.sizing} K={k:g}")
    print(f"trace: seed {cfg.seed}, {cfg.base_rate_per_min:g} req/min, minutes 0-{args.minutes - 1}; "
          f"{len(schedule)} requests; {len(plan)} ticks (minutes {args.start_minute}-{args.minutes - 1})")
    print(f"wall time: {wall_min:.1f} min of sending + ~{args.settle_s / 60:.0f} min settle + drain")
    print("first 20 sends:  wall_offset_s  request_id  minute  true_archetype          service_s -> slot held (s)")
    for row in schedule.head(20).itertuples():
        print(f"  t+{row.arrival_s / k:9.3f}   {row.request_id:>6}   {row.minute:>4}   {row.true_archetype:<22} "
              f"{row.service_s:7.3f} -> {row.service_s / k:7.3f}")
    print(f"tick times (minute: wall offset s): "
          + ", ".join(f"{m}: {due:.1f}" for m, due in plan[:4]) + f", ... , {plan[-1][0]}: {plan[-1][1]:.1f}")
    for minute, due in plan:
        print(f"  tick minute {minute:>3} at t+{due:8.2f}s")


def live_run(args: argparse.Namespace, schedule: pd.DataFrame, run_id: str, cfg: TraceConfig) -> int:
    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    stack = resolve_stack(session.client("cloudformation"), args.stack_name)
    # total_max_attempts=1: a slow tick must never be silently invoked twice.
    lam = session.client("lambda", config=Config(read_timeout=125, retries={"total_max_attempts": 1}))
    sqs, ddb = session.client("sqs"), session.client("dynamodb")
    post = http_post(f"{stack.api_url}/classify")

    logger.info("run %s: purge queues, reset pools, warm up", run_id)
    purge_queues(sqs, stack.queue_urls)
    invoke_controller(lam, stack.controller, {"reset": {"policy": args.policy, "run_id": run_id}})
    invoke_controller(lam, stack.controller, {"warmup": True})
    warm_router(post, args.warm_router)
    logger.info("settling %d s (SQS purge takes up to 60 s; mapping updates take 5-10 s)", args.settle_s)
    time.sleep(args.settle_s)
    verify_pools_reset(lam)

    def tick(minute: int) -> dict[str, Any]:
        return invoke_controller(
            lam, stack.controller,
            {"run_id": run_id, "policy": args.policy, "sizing": args.sizing, "minute": minute},
        )

    runner = Runner(
        schedule, run_id=run_id, policy=args.policy, time_scale=args.time_scale, minutes=args.minutes,
        start_minute=args.start_minute, post=post, tick=tick, workers=args.workers, max_retries=args.max_retries,
    )
    result = runner.run()

    logger.info("sending finished (aborted=%s); waiting for the queues to drain", result.aborted)
    drained = wait_for_drain(sqs, stack.queue_urls)
    if not drained:
        logger.error("queues did not drain within %d s; exporting what exists", DRAIN_TIMEOUT_S)
    export_counts = export_tables(ddb, stack.tables, run_id, LIVE_DIR)
    meta = {
        "run_id": run_id, "policy": args.policy, "sizing": args.sizing, "time_scale": args.time_scale,
        "minutes": args.minutes, "start_minute": args.start_minute, "base_rate_per_min": cfg.base_rate_per_min,
        "trace_seed": cfg.seed, "arrival_seed": ARRIVAL_SEED, "slots_per_unit": 2, "drained": drained,
        "t0_epoch": result.t0_epoch, "n_scheduled": len(schedule), "stack": args.stack_name,
        "max_retries": args.max_retries, "workers": args.workers,
    }
    write_driver_files(result, run_id, LIVE_DIR, meta)
    summary = summarise(result, args.time_scale, export_counts)
    print(json.dumps({"run_id": run_id, **summary}, indent=2, default=str))
    return 130 if result.aborted else (0 if drained else 2)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay the seed-42 trace against the live stack (Addendum §11.6)")
    p.add_argument("--policy", choices=["baseline", "aware"], required=True)
    p.add_argument("--sizing", choices=["round", "ceil"], required=True)
    p.add_argument("--time-scale", type=float, required=True, help="K: divides arrival and service times")
    p.add_argument("--minutes", type=int, required=True, help="simulated minutes to replay (<= 120)")
    p.add_argument("--dry-run", action="store_true", help="print the schedule; touch nothing in AWS")
    p.add_argument("--base-rate-per-min", type=float, default=None,
                   help="override the trace rate (the T11.1 quota fallback is 25); default: config value")
    p.add_argument("--start-minute", type=int, default=int(EVALUATION_DEFAULTS["start_minute"]))
    p.add_argument("--stack-name", default=DEFAULT_STACK)
    p.add_argument("--profile", default=DEFAULT_PROFILE)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--workers", type=int, default=SEND_WORKERS)
    p.add_argument("--max-retries", type=int, default=2)
    p.add_argument("--warm-router", type=int, default=6, help="concurrent router containers to pre-warm")
    p.add_argument("--settle-s", type=int, default=PURGE_COOLDOWN_S)
    args = p.parse_args(argv)
    if args.time_scale <= 0:
        p.error("--time-scale must be positive")
    if not 1 <= args.minutes <= 120:
        p.error("--minutes must be in 1..120")
    if args.start_minute >= args.minutes:
        p.error("--start-minute must be < --minutes")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    cfg = make_config(args.base_rate_per_min)
    schedule = select_minutes(build_schedule(cfg), args.minutes)
    run_id = new_run_id(args.policy, args.sizing)
    if args.dry_run:
        dry_run(schedule, run_id, args, cfg)
        return 0
    return live_run(args, schedule, run_id, cfg)


if __name__ == "__main__":
    sys.exit(main())
