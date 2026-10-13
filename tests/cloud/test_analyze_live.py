"""Tests for the live analysis — T13.4 (Addendum §13 "Live vs sim"), on a synthetic fixture.

The fixture manufactures a "live" run from the simulator itself: every request's timestamps are
the simulated ones divided by K, plus a known 0.3 s real polling delay. The correct answers are
therefore known exactly: with K=2 every live response must exceed the simulated one by 0.6
simulated seconds, whatever the plumbing in between. No AWS, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from cloud import analyze_live as al
from cloud import replay
from src.config import EVALUATION_DEFAULTS, PRIMITIVE_MAP
from src.controller.baseline_policy import BASELINE_POOL
from src.cost_governance.budget_projector import check_budget
from src.evaluation.latency_model import ARRIVAL_SEED, simulate_policy_latency
from src.evaluation.run_comparison import _blended_unit_cost, simulate

K = 2.0
POLL_REAL_S = 0.3  # a real-seconds polling delay added to every request
BASE_RATE = 30.0
T0 = 1_800_000_000.0
EXPECTED_SHIFT_S = POLL_REAL_S * K  # 0.6 simulated seconds


def _breach_flags(results: pd.DataFrame, policy: str) -> pd.Series:
    budget = float(EVALUATION_DEFAULTS["budget"])
    if policy == "aware":
        return results["aware_breach_projected"].astype(bool)
    blended = _blended_unit_cost(int(EVALUATION_DEFAULTS["baseline_per_unit_capacity"]))
    projection = results["baseline_actual_spend"] + results["baseline_capacity"] * blended * (120 - results["minute"])
    return projection.map(lambda v: check_budget(float(v), budget)["breach_projected"])


def make_run(live_dir: Path, policy: str, sizing: str, *, aborted: bool = False, stamp: str = "20261013000000") -> str:
    cfg = replay.make_config(BASE_RATE)
    results, labeled = simulate(cfg, sizing=sizing, recalibrate=False)
    per_request = simulate_policy_latency(labeled, results, policy, seed=ARRIVAL_SEED)  # type: ignore[arg-type]
    run_id = f"{policy}-{sizing}-{stamp}"

    service = labeled.set_index("request_id")
    ingress = T0 + per_request["arrival_s"] / K + 0.05
    start = ingress + per_request["wait_s"] / K + POLL_REAL_S
    live = pd.DataFrame({
        "run_id": run_id, "request_id": per_request["request_id"], "pool": per_request["pool"],
        "true_archetype": per_request["true_archetype"], "predicted_archetype": per_request["predicted_archetype"],
        "service_s": per_request["service_s"], "ingress_ts": ingress, "start_ts": start,
        "end_ts": start + per_request["service_s"] / K, "expires_at": 0,
    })
    flags = _breach_flags(results, policy)
    pools = [BASELINE_POOL] if policy == "baseline" else list(PRIMITIVE_MAP.values())
    decisions = pd.DataFrame({
        "run_id": run_id, "minute": results["minute"], "policy": policy, "sizing": sizing,
        "capacity": [
            json.dumps({BASELINE_POOL: int(r.baseline_capacity)} if policy == "baseline"
                       else {p: int(getattr(r, f"aware_capacity_{p}")) for p in pools})
            for r in results.itertuples()
        ],
        "breach_projected": flags.to_numpy(),
    })
    driver = pd.DataFrame({
        "request_id": per_request["request_id"], "minute": per_request["minute"], "planned_s": 0.0, "actual_s": 0.0,
        "lateness_s": 0.01, "status": 202, "rtt_s": 0.1, "attempts": 1, "first_status": 202, "error": "",
        "run_aborted": aborted,
    })
    ticks = pd.DataFrame({"minute": results["minute"], "planned_s": 0.0, "actual_s": 0.0, "latency_s": 3.0, "status": 200,
                          "function_error": "", "changes": "{}", "outstanding_sends": 0})
    meta = {"run_id": run_id, "policy": policy, "sizing": sizing, "time_scale": K, "minutes": 120, "start_minute": 10,
            "base_rate_per_min": BASE_RATE, "n_scheduled": len(per_request), "aborted": aborted, "drained": True}

    live.to_csv(live_dir / f"{run_id}_results.csv", index=False)
    decisions.to_csv(live_dir / f"{run_id}_decisions.csv", index=False)
    driver.to_csv(live_dir / f"{run_id}_driver.csv", index=False)
    ticks.to_csv(live_dir / f"{run_id}_ticks.csv", index=False)
    (live_dir / f"{run_id}_meta.json").write_text(json.dumps(meta))
    return run_id


@pytest.fixture(scope="module")
def live_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("live")
    make_run(d, "aware", "ceil")
    make_run(d, "baseline", "ceil")
    return d


@pytest.fixture(scope="module")
def aware(live_dir: Path) -> dict[str, Any]:
    return al.analyse_run(al.load_run("aware-ceil-20261013000000", live_dir))


@pytest.fixture(scope="module")
def baseline(live_dir: Path) -> dict[str, Any]:
    return al.analyse_run(al.load_run("baseline-ceil-20261013000000", live_dir))


# --------------------------------------------------------------------------------
# Known-answer checks
# --------------------------------------------------------------------------------


def test_every_live_response_exceeds_the_simulated_one_by_exactly_k_times_the_polling_delay(aware: dict[str, Any]) -> None:
    err = aware["paired_response_error_vs_sim_spec_s"]
    assert err["n"] == aware["counts"]["results_rows"]
    assert err["median"] == pytest.approx(EXPECTED_SHIFT_S, abs=1e-6)
    assert err["mean"] == pytest.approx(EXPECTED_SHIFT_S, abs=1e-6) and err["p95"] == pytest.approx(EXPECTED_SHIFT_S, abs=1e-6)


def test_per_pool_percentiles_are_shifted_by_the_same_constant(aware: dict[str, Any]) -> None:
    pools = aware["per_pool_live_vs_sim_spec"]
    assert set(pools) <= set(PRIMITIVE_MAP.values()) and len(pools) >= 3
    for pool, stats in pools.items():
        for quantile in ("p50", "p95"):
            assert stats[quantile]["abs_err"] == pytest.approx(EXPECTED_SHIFT_S, abs=1e-6), (pool, quantile)
            assert stats[quantile]["rel_err"] == pytest.approx(EXPECTED_SHIFT_S / stats[quantile]["sim"], abs=1e-6)
        assert stats["n_live"] == stats["n_sim"]


def test_the_live_summary_is_the_simulations_own_summariser_output(aware: dict[str, Any]) -> None:
    """Same keys and windows as latency_summary.json, because summarise_latency is reused."""
    live = aware["live"]
    assert set(live) == {"overall", "by_archetype", "by_window"} == set(aware["sim_spec_timeline"])
    assert set(live["by_window"]) == {"pre", "during", "post"}
    assert live["overall"]["n"] == aware["counts"]["results_rows"]
    # +0.6 s on every response can only move a handful of borderline requests across their SLA
    assert abs(aware["sla_live_vs_sim_spec"]["overall"]["abs_err"]) < 0.02
    assert aware["sla_live_vs_sim_spec"]["post"]["sim"] == aware["sim_spec_timeline"]["by_window"]["post"]["sla_violation_rate"]


def test_the_delayed_timeline_is_a_genuinely_different_simulation(aware: dict[str, Any]) -> None:
    spec, delayed = aware["sim_spec_timeline"], aware["sim_decision_delayed_one_minute"]
    assert spec != delayed
    assert delayed["overall"]["n"] == spec["overall"]["n"]


def test_counts_and_a_clean_run_have_no_losses(aware: dict[str, Any], baseline: dict[str, Any]) -> None:
    for a in (aware, baseline):
        c = a["counts"]
        assert c["lost"] == 0 and c["non_2xx"] == 0 and c["retried"] == 0 and not c["aborted"]
        assert c["sent"] == c["accepted_2xx"] == c["results_rows"] == c["scheduled"]
        assert c["tick_failures"] == 0 and c["ticks"] == 110
        assert a["predictions_filled_from_local_classifier"] == 0


def test_governance_first_breach_matches_between_live_and_sim_and_the_lead_time_is_computed(
    aware: dict[str, Any], baseline: dict[str, Any]
) -> None:
    for a in (aware, baseline):
        assert a["governance"]["first_breach_minute_live"] == a["governance"]["first_breach_minute_sim"]
    lead = al.governance_lead_time([aware, baseline])["ceil"]
    expected = (None if None in (baseline["governance"]["first_breach_minute_sim"], aware["governance"]["first_breach_minute_sim"])
                else baseline["governance"]["first_breach_minute_sim"] - aware["governance"]["first_breach_minute_sim"])
    assert lead["live_lead_min"] == lead["sim_lead_min"] == expected


def test_baseline_run_uses_the_homogeneous_pool_only(baseline: dict[str, Any]) -> None:
    assert set(baseline["per_pool_live_vs_sim_spec"]) == {BASELINE_POOL}


# --------------------------------------------------------------------------------
# Robustness: lost requests, aborted runs, the delayed-timeline helper
# --------------------------------------------------------------------------------


def test_lost_requests_are_counted_and_their_predictions_filled(tmp_path: Path, live_dir: Path) -> None:
    run_id = "aware-ceil-20261013000000"
    for f in live_dir.glob(f"{run_id}_*"):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    results = pd.read_csv(tmp_path / f"{run_id}_results.csv")
    pd.concat([results.iloc[:100], results.iloc[105:]]).to_csv(tmp_path / f"{run_id}_results.csv", index=False)

    analysis = al.analyse_run(al.load_run(run_id, tmp_path))

    assert analysis["counts"]["lost"] == 5 and analysis["predictions_filled_from_local_classifier"] == 5
    assert analysis["paired_response_error_vs_sim_spec_s"]["n"] == len(results) - 5
    # the arrival stream stayed aligned despite the gap: the remaining requests still differ by exactly 0.6
    assert analysis["paired_response_error_vs_sim_spec_s"]["median"] == pytest.approx(EXPECTED_SHIFT_S, abs=1e-6)


def test_decision_capacity_frame_shifts_decisions_by_the_requested_delay() -> None:
    decisions = pd.DataFrame({"minute": [10, 11], "capacity": [json.dumps({BASELINE_POOL: 2}), json.dumps({BASELINE_POOL: 3})]})
    assert al.decision_capacity_frame(decisions, "baseline")["minute"].tolist() == [10, 11]
    shifted = al.decision_capacity_frame(decisions, "baseline", delay_minutes=1)
    assert shifted["minute"].tolist() == [11, 12] and shifted["baseline_capacity"].tolist() == [2, 3]
    aware = al.decision_capacity_frame(
        pd.DataFrame({"minute": [10], "capacity": [json.dumps({"eks_gpu_reserved": 2})]}), "aware")
    assert aware["aware_capacity_eks_gpu_reserved"].tolist() == [2] and aware["aware_capacity_bedrock_on_demand"].tolist() == [0]


def test_rolling_violation_rate_is_a_five_minute_mean() -> None:
    per_request = pd.DataFrame({"minute": [0, 0, 1, 2, 3, 4, 5], "sla_violated": [True, False, True, True, False, False, True]})
    rolling = al.rolling_violation_rate(per_request, minutes=6)
    assert rolling.iloc[0] == 0.5 and rolling.iloc[2] == pytest.approx((0.5 + 1 + 1) / 3)
    assert rolling.iloc[5] == pytest.approx(np.mean([1.0, 1.0, 0.0, 0.0, 1.0]))


def test_cli_writes_summary_and_both_plots_and_skips_aborted_runs(tmp_path: Path, live_dir: Path) -> None:
    for f in live_dir.iterdir():
        (tmp_path / f.name).write_bytes(f.read_bytes())
    make_run(tmp_path, "aware", "round", aborted=True, stamp="20261013111111")

    assert al.main(["--live-dir", str(tmp_path)]) == 0

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert {r["run_id"] for r in summary["runs"]} == {"aware-ceil-20261013000000", "baseline-ceil-20261013000000"}
    assert "ceil" in summary["governance_lead_time"]
    assert all("_frames" not in r for r in summary["runs"])
    for png in ("live_vs_sim.png", "latency_timeline.png"):
        assert (tmp_path / png).stat().st_size > 10_000, png


def test_cli_with_no_runs_fails_clearly(tmp_path: Path) -> None:
    assert al.main(["--live-dir", str(tmp_path)]) == 1
