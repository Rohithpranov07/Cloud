"""Tests for the T13.2 calibration script. Offline: simulated pool, fake AWS clients."""

from __future__ import annotations

import math
import random
from email.utils import formatdate
from typing import Any

import pytest

from cloud import calibrate, replay


def simulate_pool(n: int, service_s: float, t_set: float, lag_s: float, low: int = 2, high: int = 8,
                  start: float = 0.0) -> list[tuple[float, float]]:
    """A pool that allows ``low`` slots until t_set + lag, then ``high`` — the ground truth the
    measurement has to recover. Greedy starts, one event at a time."""
    intervals: list[tuple[float, float]] = []
    change = t_set + lag_s
    t = start
    pending = n
    while pending:
        limit = high if t >= change else low
        running = [e for _, e in intervals if e > t]
        free = limit - len(running)
        for _ in range(max(0, min(free, pending))):
            intervals.append((t, t + service_s))
            pending -= 1
        if pending:
            nxt = [e for e in (e for _, e in intervals) if e > t]
            candidates = ([min(nxt)] if nxt else []) + ([change] if t < change else [])
            t = min(candidates)
    return intervals


# --------------------------------------------------------------------------------
# Pure maths
# --------------------------------------------------------------------------------


def test_percentile_is_nearest_rank() -> None:
    assert calibrate.percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert calibrate.percentile([1, 2, 3, 4, 5], 0.95) == 5
    assert math.isnan(calibrate.percentile([], 0.5))


def test_concurrency_helpers() -> None:
    intervals = [(0.0, 6.0), (0.0, 6.0), (6.0, 12.0), (6.0, 12.0)]  # back-to-back at t=6: never 3 at once
    assert calibrate.max_concurrency(intervals) == 2
    assert calibrate.active_count_at(intervals, 5.9) == 2 and calibrate.active_count_at(intervals, 6.0) == 2
    assert calibrate.first_time_reaching([(0, 5), (2, 6), (3, 7)], 3, after=0) == 3
    assert calibrate.first_time_reaching([(0, 5), (2, 6), (3, 7)], 3, after=3.5) is None


@pytest.mark.parametrize("true_lag", [2.0, 7.0, 13.5])
def test_actuation_lag_recovers_a_known_lag(true_lag: float) -> None:
    t_set = 25.0
    intervals = simulate_pool(60, 6.0, t_set, true_lag)
    result = calibrate.actuation_lag(intervals, t_set)
    assert result["lag_to_target_s"] == pytest.approx(true_lag, abs=1e-9)
    assert result["peak_before_change"] == 2.0  # the old limit was respected before the change
    assert result["peak_after_change"] == 8.0
    assert result["lag_to_first_extra_slot_s"] == pytest.approx(true_lag, abs=1e-9)


def test_actuation_lag_is_none_when_the_pool_never_reaches_the_target() -> None:
    intervals = simulate_pool(60, 6.0, 25.0, lag_s=1e9)  # concurrency never rises
    assert calibrate.actuation_lag(intervals, 25.0)["lag_to_target_s"] is None


@pytest.mark.parametrize(
    ("p95", "expected"),
    [(0.0, 6), (2.4, 6), (2.5, 4), (3.7, 4), (3.75, 3), (4.9, 3), (5.0, 2), (7.4, 2), (7.5, 1), (14.9, 1), (15.0, None), (40.0, None)],
)
def test_time_scale_rule_picks_the_largest_k_whose_threshold_holds(p95: float, expected: int | None) -> None:
    """Thresholds 0.25 x 60 / K: K=1 15 s, 2 7.5 s, 3 5 s, 4 3.75 s, 6 2.5 s (strict <)."""
    assert calibrate.choose_time_scale(p95) == expected


def test_clock_skew_estimate_recovers_a_known_skew() -> None:
    rng = random.Random(3)
    true_skew = 3.2
    samples = []
    for _ in range(400):
        aws_true = 1_700_000_000 + rng.uniform(0, 1000)
        local = aws_true + true_skew + 0.04  # laptop ahead by 3.2 s, ~40 ms of latency
        samples.append((local, math.floor(aws_true)))  # Date header is floor'd to the second
    assert calibrate.clock_skew_s(samples) == pytest.approx(true_skew, abs=0.1)


def test_report_durations_keeps_only_warm_invocations() -> None:
    messages = [
        "REPORT RequestId: a\tDuration: 25.50 ms\tBilled Duration: 26 ms\tMemory Size: 1024 MB\tMax Memory Used: 320 MB",
        "REPORT RequestId: b\tDuration: 10000.00 ms\tBilled Duration: 10000 ms\tMax Memory Used: 177 MB\tStatus: timeout",
        "REPORT RequestId: c\tDuration: 1300.00 ms\tInit Duration: 900.00 ms",
        "START RequestId: d",
        "REPORT RequestId: e\tDuration: 14.46 ms\tBilled Duration: 15 ms",
    ]
    assert calibrate.report_durations_ms(messages) == [25.5, 14.46]


def test_aws_epoch_parses_the_date_header() -> None:
    header = formatdate(1_700_000_123, usegmt=True)
    assert calibrate.aws_epoch({"ResponseMetadata": {"HTTPHeaders": {"date": header}}}) == 1_700_000_123


# --------------------------------------------------------------------------------
# Orchestration against fakes
# --------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeLambda:
    def __init__(self, clock: FakeClock) -> None:
        self.clock, self.value, self.updates = clock, 2, []

    def get_event_source_mapping(self, UUID: str) -> dict:
        return {"State": "Enabled", "ScalingConfig": {"MaximumConcurrency": self.value}}

    def update_event_source_mapping(self, UUID: str, ScalingConfig: dict) -> dict:
        self.value = ScalingConfig["MaximumConcurrency"]
        self.updates.append((self.clock.t, self.value))
        return {"ResponseMetadata": {"HTTPHeaders": {"date": formatdate(int(self.clock.t), usegmt=True)}}}


class FakeSqs:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def send_message_batch(self, QueueUrl: str, Entries: list[dict]) -> dict:
        self.entries.extend(Entries)
        return {"Successful": [{"Id": e["Id"]} for e in Entries]}


def test_preload_sends_sixty_valid_worker_messages_in_batches_of_ten() -> None:
    sqs = FakeSqs()
    assert calibrate.preload_messages(sqs, "q", "calib-x", rep=2, now=lambda: 1.5) == 60
    assert len(sqs.entries) == 60 and len({e["Id"] for e in sqs.entries[:10]}) == 10
    from cloud.handlers import worker

    parsed = [worker.parse_message(e["MessageBody"]) for e in sqs.entries]  # the real worker accepts them
    assert {p["service_s"] for p in parsed} == {6.0} and len({p["request_id"] for p in parsed}) == 60
    assert min(p["request_id"] for p in parsed) == 2000


def test_a_rejected_batch_raises() -> None:
    class Bad:
        def send_message_batch(self, **_: Any) -> dict:
            return {"Failed": [{"Id": "0"}]}

    with pytest.raises(RuntimeError, match="send_message_batch failed"):
        calibrate.preload_messages(Bad(), "q", "r", 1)


def test_actuation_repetition_measures_the_lag_of_a_simulated_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    lam, sqs = FakeLambda(clock), FakeSqs()
    holder: dict[str, Any] = {}

    def fake_query(ddb: Any, table: str, run_id: str) -> list[dict]:
        t_set = holder.setdefault("t_set", float(lam.updates[0][0]))
        return [{"start_ts": s, "end_ts": e} for s, e in simulate_pool(60, 6.0, t_set, lag_s=6.0, start=t_set - 25)]

    monkeypatch.setattr(replay, "query_run", fake_query)
    rep = calibrate.run_actuation_rep(
        rep=1, sqs=sqs, lam=lam, ddb=None, queue_url="q", mapping_uuid="u", results_table="t",
        now=clock.now, sleep=clock.sleep,
    )

    assert rep["lag_to_target_s"] == pytest.approx(6.0)
    assert rep["peak_before_change"] == 2.0
    assert [v for _, v in lam.updates] == [8, 2]  # raised, then restored for the next repetition
    assert len(sqs.entries) == 60


def test_router_probes_apply_the_skew_correction(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    skew, true_overhead, poll = 2.5, 0.12, 1.7
    sent: list[float] = []

    def post(body: dict) -> tuple[int, str]:
        sent.append(clock.now())
        return 202, ""

    def fake_query(ddb: Any, table: str, run_id: str) -> list[dict]:
        return [
            {"request_id": i, "ingress_ts": t - skew + true_overhead, "start_ts": t - skew + true_overhead + poll}
            for i, t in enumerate(sent)
        ]

    monkeypatch.setattr(replay, "query_run", fake_query)
    out = calibrate.run_router_probes(
        post=post, ddb=None, results_table="t", skew_s=skew, n=6, gap_s=10.0, now=clock.now, sleep=clock.sleep,
        template={"policy": "aware", "minute": 0},
    )
    assert len(out["rows"]) == 6
    assert all(r["router_overhead_s"] == pytest.approx(true_overhead) for r in out["rows"])
    assert all(r["polling_delay_s"] == pytest.approx(poll) for r in out["rows"])


def test_a_failed_probe_aborts_the_calibration() -> None:
    with pytest.raises(RuntimeError, match="HTTP 500"):
        calibrate.run_router_probes(
            post=lambda b: (500, "cold"), ddb=None, results_table="t", skew_s=0.0, n=2, gap_s=0.0,
            sleep=lambda s: None, template={},
        )


def test_read_router_durations_follows_pagination() -> None:
    class FakeLogs:
        calls: list[dict] = []

        def filter_log_events(self, **kwargs: Any) -> dict:
            self.calls.append(kwargs)
            if "nextToken" not in kwargs:
                return {"events": [{"message": "REPORT RequestId: a\tDuration: 20.00 ms"}], "nextToken": "t2"}
            return {"events": [{"message": "REPORT RequestId: b\tDuration: 30.00 ms"}]}

    assert calibrate.read_router_durations(FakeLogs(), since_epoch_s=100.0) == [20.0, 30.0]
    assert FakeLogs.calls[0]["startTime"] == 100_000 and FakeLogs.calls[0]["logGroupName"] == "/aws/lambda/semauto-router"


def test_report_chooses_k_and_fails_closed_when_a_rep_never_reaches_eight() -> None:
    def rep(lag: float | None) -> dict:
        return {"lag_to_target_s": lag, "lag_to_first_extra_slot_s": lag, "peak_before_change": 2.0}

    probes = [{"router_overhead_s": 0.1, "rtt_s": 0.2, "polling_delay_s": 1.0}]
    good = calibrate.calibration_report([rep(4.0), rep(4.5), rep(3.5), rep(4.0), rep(4.8)], probes, [20.0], 1.0)
    assert good["actuation_lag_to_8_s"]["p95"] == 4.8 and good["chosen_time_scale"] == 3  # 4.8 < 5 s (K=3), not < 3.75 s (K=4)

    bad = calibrate.calibration_report([rep(4.0), rep(None), rep(3.5)], probes, [20.0], 1.0)
    assert bad["actuation_lag_to_8_s"]["reps_that_never_reached_8"] == 1 and bad["chosen_time_scale"] is None
    assert good["k_thresholds_s"]["1"] == 15.0 and good["k_thresholds_s"]["6"] == 2.5
