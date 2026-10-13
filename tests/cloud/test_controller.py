"""Tests for the controller Lambda — T12.4 (Addendum §11.4, §13 "Cloud unit tests").

Real boto3 DynamoDB/Lambda/SNS clients wrapped in Stubbers with exact expected
parameters; no network and no AWS credentials (NFR-A4).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
import pytest
from botocore.stub import ANY, Stubber

from cloud.common import POOLS, load_settings
from cloud.handlers import controller
from src.config import EVALUATION_DEFAULTS, PRIMITIVE_MAP
from src.controller.baseline_policy import BASELINE_POOL
from src.cost_governance.budget_projector import check_budget
from src.evaluation.run_comparison import _blended_unit_cost, simulate
from src.trace_gen.generator import _default_config

START_MINUTE = int(EVALUATION_DEFAULTS["start_minute"])
NOW = 1_700_000_000.0
TOPIC = "arn:aws:sns:ap-south-1:000000000000:semauto-alerts"
ESM_UUID = {pool: str(uuid.uuid5(uuid.NAMESPACE_URL, f"semauto/{pool}")) for pool in POOLS}
SETTINGS = load_settings(
    {
        "COUNTS_TABLE": "semauto-counts",
        "DECISIONS_TABLE": "semauto-decisions",
        "RESULTS_TABLE": "semauto-results",
        **{f"QUEUE_URL_{p.upper()}": f"https://sqs.example/{p}" for p in POOLS},
        **{f"ESM_UUID_{p.upper()}": ESM_UUID[p] for p in POOLS},
    }
)


@pytest.fixture(autouse=True)
def no_aws_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))


# --------------------------------------------------------------------------------
# Helpers: stubbed clients and DynamoDB-shaped telemetry
# --------------------------------------------------------------------------------


def _counts_by_minute(labeled: pd.DataFrame) -> dict[int, dict[str, Any]]:
    """Low-level semauto-counts items, exactly as the router's ADD counters leave them."""
    counts = labeled.groupby(["minute", "predicted_archetype"]).size().unstack(fill_value=0)
    items: dict[int, dict[str, Any]] = {}
    for minute, row in counts.iterrows():
        item: dict[str, Any] = {"run_id": {"S": "r"}, "minute": {"N": str(int(minute))}}
        item.update({a: {"N": str(int(row[a]))} for a in counts.columns if int(row[a]) > 0})
        items[int(minute)] = item
    return items


class Clients:
    def __init__(self) -> None:
        self.ddb = boto3.client("dynamodb", region_name="ap-south-1")
        self.lam = boto3.client("lambda", region_name="ap-south-1")
        self.sns = boto3.client("sns", region_name="ap-south-1")
        self.stubs = [Stubber(self.ddb), Stubber(self.lam), Stubber(self.sns)]
        self.emf: list[dict[str, Any]] = []

    def __enter__(self) -> Clients:
        for stub in self.stubs:
            stub.activate()
        return self

    def __exit__(self, *exc: object) -> None:
        for stub in self.stubs:
            stub.deactivate()

    def assert_drained(self) -> None:
        for stub in self.stubs:
            stub.assert_no_pending_responses()

    def expect_tick_reads(self, run_id: str, minute: int, counts: list[dict], decisions: list[dict]) -> None:
        ddb = self.stubs[0]
        for table, op, items in (("semauto-counts", "<=", counts), ("semauto-decisions", "<", decisions)):
            ddb.add_response(
                "query",
                {"Items": items},
                {
                    "TableName": table,
                    "KeyConditionExpression": f"run_id = :r AND #m {op} :m",
                    "ExpressionAttributeNames": {"#m": "minute"},
                    "ExpressionAttributeValues": {":r": {"S": run_id}, ":m": {"N": str(minute)}},
                    "ConsistentRead": True,
                },
            )

    def expect_actuation(self, pool: str, value: int) -> None:
        self.stubs[1].add_response(
            "update_event_source_mapping",
            {"UUID": ESM_UUID[pool]},
            {"UUID": ESM_UUID[pool], "ScalingConfig": {"MaximumConcurrency": value}},
        )

    def expect_put(self) -> None:
        self.stubs[0].add_response("put_item", {}, {"TableName": "semauto-decisions", "Item": ANY})

    def expect_publish(self) -> None:
        self.stubs[2].add_response(
            "publish", {"MessageId": "m-1"}, {"TopicArn": TOPIC, "Subject": ANY, "Message": ANY}
        )

    def tick(self, event: dict[str, Any]) -> dict[str, Any]:
        return controller.tick(
            event, self.ddb, self.lam, self.sns, SETTINGS,
            topic_arn=TOPIC, now=lambda: NOW, emit=lambda *a, **k: self.emf.append({"args": a, "kwargs": k}),
        )


@pytest.fixture(scope="module")
def no_recalibration() -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    """simulate(recalibrate=False) for both sizing rules — what the live system must match."""
    return {sizing: simulate(_default_config(), sizing=sizing, recalibrate=False) for sizing in ("round", "ceil")}


def _expected_capacity(results: pd.DataFrame, policy: str, minute: int) -> dict[str, int]:
    row = results.set_index("minute").loc[minute]
    if policy == "baseline":
        return {BASELINE_POOL: int(row["baseline_capacity"])}
    return {p: int(row[f"aware_capacity_{p}"]) for p in PRIMITIVE_MAP.values()}


def _queue_actuations(clients: Clients, capacity: dict[str, int], actuated: dict[str, int]) -> None:
    """Queue exactly the ESM updates the rule implies; update ``actuated`` in place."""
    for pool, units in capacity.items():
        target = max(2, 2 * units)
        if target != actuated[pool]:
            clients.expect_actuation(pool, target)
            actuated[pool] = target


def _decision_item(minute: int, capacity: dict[str, int], actuated: dict[str, int], notified: bool) -> dict:
    return {
        "run_id": {"S": "r"},
        "minute": {"N": str(minute)},
        "capacity": {"M": {p: {"N": str(u)} for p, u in capacity.items()}},
        "actuated": {"M": {p: {"N": str(v)} for p, v in actuated.items()}},
        "notified": {"BOOL": notified},
    }


# --------------------------------------------------------------------------------
# Actuation rule
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(("units", "expected"), [(0, 2), (1, 2), (2, 4), (3, 6), (7, 14)])
def test_actuation_rule_is_max_of_two_and_two_slots_per_unit(units: int, expected: int) -> None:
    assert controller.max_concurrency(units, slots_per_unit=2) == expected


def test_actuation_above_the_esm_limit_raises() -> None:
    with pytest.raises(ValueError, match="1000"):
        controller.max_concurrency(501, slots_per_unit=2)


def test_tick_actuates_changed_pools_with_the_rule(no_recalibration: dict) -> None:
    """Aware, ceil, minute 75 after a history at 1 unit/pool: every changed pool is
    actuated to exactly max(2, 2 x units)."""
    results, labeled = no_recalibration["ceil"]
    minute = 75
    counts = [item for m, item in sorted(_counts_by_minute(labeled).items()) if m <= minute]
    ones = {p: 1 for p in PRIMITIVE_MAP.values()}
    previous = [_decision_item(minute - 1, ones, {p: 2 for p in ones}, False)]
    expected = {p: int(results.set_index("minute").loc[minute, f"aware_capacity_{p}"]) for p in ones}

    with Clients() as clients:
        clients.expect_tick_reads("r", minute, counts, previous)
        for pool in PRIMITIVE_MAP.values():
            target = max(2, 2 * expected[pool])
            if target != 2:
                clients.expect_actuation(pool, target)
        clients.expect_put()
        result = clients.tick({"run_id": "r", "policy": "aware", "sizing": "ceil", "minute": minute})
        clients.assert_drained()

    assert result["capacity"] == expected
    assert all(value == max(2, 2 * expected[p]) for p, value in result["changes"].items())


def test_no_actuation_call_when_nothing_changed(no_recalibration: dict) -> None:
    """Previous decision already actuated at today's targets: zero Lambda API calls."""
    results, labeled = no_recalibration["round"]
    minute = 30
    capacity = {BASELINE_POOL: int(results.set_index("minute").loc[minute, "baseline_capacity"])}
    target = {BASELINE_POOL: max(2, 2 * capacity[BASELINE_POOL])}
    counts = [item for m, item in sorted(_counts_by_minute(labeled).items()) if m <= minute]

    with Clients() as clients:
        clients.expect_tick_reads("r", minute, counts, [_decision_item(minute - 1, capacity, target, False)])
        clients.expect_put()  # no update_event_source_mapping queued: any call would raise
        result = clients.tick({"run_id": "r", "policy": "baseline", "sizing": "round", "minute": minute})
        clients.assert_drained()

    assert result["changes"] == {}


# --------------------------------------------------------------------------------
# First tick, SNS once, reset, warmup, EMF
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["aware", "baseline"])
def test_first_tick_starts_from_the_initial_capacity(
    policy: str, no_recalibration: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    results, labeled = no_recalibration["ceil"]
    seen: dict[str, Any] = {}
    real_decide = controller.decide

    def spy(policy_: str, sizing: str, frame: Any, previous_capacity: dict) -> Any:
        seen["previous_capacity"] = dict(previous_capacity)
        return real_decide(policy_, sizing, frame, previous_capacity)

    monkeypatch.setattr(controller, "decide", spy)
    counts = [item for m, item in sorted(_counts_by_minute(labeled).items()) if m <= START_MINUTE]
    expected = _expected_capacity(results, policy, START_MINUTE)

    with Clients() as clients:
        clients.expect_tick_reads("r", START_MINUTE, counts, [])  # no previous decision
        # Actuation starts from the reset value (2 per pool): only pools that move off it.
        _queue_actuations(clients, expected, {p: 2 for p in expected})
        clients.expect_put()
        result = clients.tick({"run_id": "r", "policy": policy, "sizing": "ceil", "minute": START_MINUTE})
        clients.assert_drained()

    assert seen["previous_capacity"] == controller.initial_capacity(policy)
    assert controller.initial_capacity(policy) == (
        {BASELINE_POOL: 1} if policy == "baseline" else {p: 1 for p in PRIMITIVE_MAP.values()}
    )
    assert result["capacity"] == expected


def test_sns_is_published_exactly_once_across_breaching_ticks(no_recalibration: dict) -> None:
    """Aware, round: drive every minute; the projection breaches at one minute and stays
    breached, but exactly one SNS publish happens for the run."""
    results, labeled = no_recalibration["round"]
    counts_by_minute = _counts_by_minute(labeled)
    breach_minutes = results.loc[results["aware_breach_projected"], "minute"].tolist()
    assert len(breach_minutes) > 1, "fixture should breach on several minutes"
    first_breach = breach_minutes[0]

    decisions: list[dict] = []
    actuated = {p: 2 for p in PRIMITIVE_MAP.values()}
    breaching_ticks = 0
    with Clients() as clients:
        for minute in range(START_MINUTE, first_breach + 6):
            counts = [item for m, item in sorted(counts_by_minute.items()) if m <= minute]
            clients.expect_tick_reads("r", minute, counts, list(decisions))
            _queue_actuations(clients, _expected_capacity(results, "aware", minute), actuated)
            clients.expect_put()
            if minute == first_breach:
                clients.expect_publish()  # the ONLY publish queued: a second one would raise
            result = clients.tick({"run_id": "r", "policy": "aware", "sizing": "round", "minute": minute})
            clients.assert_drained()
            decisions.append(result["decision_item"])
            breaching_ticks += int(result["breach_projected"])
            assert result["notified"] == (minute >= first_breach)

    assert breaching_ticks == 6  # breached on six consecutive ticks, published once


def test_reset_sets_every_esm_to_its_initial_value() -> None:
    with Clients() as clients:
        for pool in (*PRIMITIVE_MAP.values(), BASELINE_POOL):
            clients.expect_actuation(pool, 2)
        result = controller.reset({"reset": {"policy": "aware", "run_id": "r"}}, clients.lam, SETTINGS)
        clients.assert_drained()
    assert result == {"reset": {pool: 2 for pool in (*PRIMITIVE_MAP.values(), BASELINE_POOL)}}


def test_reset_rejects_a_malformed_payload() -> None:
    with pytest.raises(ValueError, match="reset needs"):
        controller.reset({"reset": {"run_id": "r"}}, None, SETTINGS)


def test_warmup_returns_without_touching_configuration_or_aws() -> None:
    assert controller.handler({"warmup": True}, None) == {"warmup": "ok"}


@pytest.mark.parametrize(
    "event",
    [
        {"policy": "aware", "sizing": "ceil", "minute": 10},
        {"run_id": "r", "policy": "both", "sizing": "ceil", "minute": 10},
        {"run_id": "r", "policy": "aware", "sizing": "floor", "minute": 10},
        {"run_id": "r", "policy": "aware", "sizing": "ceil", "minute": 120},
        {"run_id": "r", "policy": "aware", "sizing": "ceil", "minute": True},
    ],
)
def test_malformed_tick_is_rejected(event: dict) -> None:
    with pytest.raises(ValueError):
        controller.parse_tick(event)


def test_tick_emits_units_forecast_and_spend_metrics(no_recalibration: dict) -> None:
    results, labeled = no_recalibration["ceil"]
    counts = [item for m, item in sorted(_counts_by_minute(labeled).items()) if m <= START_MINUTE]
    with Clients() as clients:
        clients.expect_tick_reads("r", START_MINUTE, counts, [])
        _queue_actuations(clients, _expected_capacity(results, "aware", START_MINUTE), {p: 2 for p in PRIMITIVE_MAP.values()})
        clients.expect_put()
        clients.tick({"run_id": "r", "policy": "aware", "sizing": "ceil", "minute": START_MINUTE})
        clients.assert_drained()

    metric_names = [next(iter(e["args"][2])) for e in clients.emf]
    assert metric_names.count("Units") == 4
    assert metric_names.count("ForecastPeak") == 4
    assert metric_names[-1] == "ProjectedSpend"
    assert set(clients.emf[-1]["args"][2]) == {"ProjectedSpend", "Budget"}


# --------------------------------------------------------------------------------
# Parity: tick() minute by minute == simulate(recalibrate=False)
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("sizing", ["round", "ceil"])
@pytest.mark.parametrize("policy", ["aware", "baseline"])
def test_tick_sequence_matches_simulate_without_recalibration(
    policy: str, sizing: str, no_recalibration: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every capacity, every actuation call, the projected spend, and the single SNS
    publish are dictated by simulate(recalibrate=False) and queued as exact Stubber
    expectations, so any divergence in tick() is an unexpected/missing AWS call.
    """
    results, labeled = no_recalibration[sizing]
    indexed = results.set_index("minute")
    counts_by_minute = _counts_by_minute(labeled)
    pools = controller.POOLS_BY_POLICY[policy]
    blended = _blended_unit_cost(int(EVALUATION_DEFAULTS["baseline_per_unit_capacity"]))
    budget = float(EVALUATION_DEFAULTS["budget"])

    def expected_capacity(minute: int) -> dict[str, int]:
        if policy == "baseline":
            return {BASELINE_POOL: int(indexed.loc[minute, "baseline_capacity"])}
        return {p: int(indexed.loc[minute, f"aware_capacity_{p}"]) for p in pools}

    def expected_projection(minute: int) -> float:
        if policy == "aware":
            return float(indexed.loc[minute, "aware_projected_spend"])
        units = int(indexed.loc[minute, "baseline_capacity"])
        return float(indexed.loc[minute, "baseline_actual_spend"]) + units * blended * (120 - minute)

    breaches = [m for m in indexed.index if check_budget(expected_projection(m), budget)["breach_projected"]]
    first_breach = breaches[0] if breaches else None

    decisions: list[dict] = []
    actuated = {p: 2 for p in pools}  # the reset value
    live_capacity: list[dict[str, int]] = []
    n_actuations = 0

    with Clients() as clients:
        for minute in indexed.index:
            counts = [item for m, item in sorted(counts_by_minute.items()) if m <= minute]
            clients.expect_tick_reads("r", int(minute), counts, list(decisions))
            for pool, units in expected_capacity(minute).items():
                target = max(2, 2 * units)
                if target != actuated[pool]:
                    clients.expect_actuation(pool, target)
                    actuated[pool] = target
                    n_actuations += 1
            clients.expect_put()
            if minute == first_breach:
                clients.expect_publish()

            result = clients.tick({"run_id": "r", "policy": policy, "sizing": sizing, "minute": int(minute)})
            clients.assert_drained()

            assert result["capacity"] == expected_capacity(minute), f"capacity diverged at minute {minute}"
            assert result["projected_spend"] == pytest.approx(expected_projection(minute), rel=1e-12)
            live_capacity.append(result["capacity"])
            decisions.append(result["decision_item"])

    with capsys.disabled():
        changes = sum(1 for a, b in zip(live_capacity, live_capacity[1:]) if a != b)
        governance = (
            f"first projected breach at minute {first_breach}, SNS published exactly once"
            if first_breach is not None
            else "never projects a breach, no SNS publish"
        )
        print(
            f"\n  parity {policy:>8}/{sizing:<5}: {len(live_capacity)} ticks (minutes "
            f"{indexed.index[0]}-{indexed.index[-1]}), capacity identical to simulate(recalibrate=False) "
            f"at every minute; {changes} tick-to-tick capacity changes, {n_actuations} ESM updates "
            f"(incl. the move off the reset value); {governance}"
        )
