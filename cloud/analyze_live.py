"""Live analysis and simulation-vs-live validation — T13.4 (Addendum §11.3, §13 "Live vs sim").

Turns the files ``cloud.replay`` exported into the comparison the report needs, and checks the
simulation against reality.

* Live response time = (end_ts - ingress_ts) x K, in simulated seconds, both timestamps being AWS
  clocks (the laptop clock never enters). SLA flags use the §9.1 SLAs and the SAME
  ``summarise_latency`` the simulation uses; there is no second summariser.
* The simulation is re-run on the same trace with the live system's exact capacity timeline and
  the live classifier's predictions for routing, so the only remaining difference is the
  infrastructure. Two timelines are reported: the one the simulator's convention implies (a
  decision for minute m applies from the START of minute m) and one delayed by a minute (a real
  controller decides at the END of minute m). The first is the specification; the second isolates
  what is left after that modelling convention is removed.
* Governance: first projected-breach minute per policy and the lead time, live vs simulation.

    python -m cloud.analyze_live
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from cloud import replay
from src.classifier.archetype_classifier import add_proxy_features, classify, train_classifier
from src.config import EVALUATION_DEFAULTS, PRIMITIVE_MAP
from src.controller.baseline_policy import BASELINE_POOL
from src.cost_governance.budget_projector import check_budget
from src.evaluation.latency_model import (
    ARRIVAL_SEED, SLA_SECONDS, simulate_policy_latency, summarise_latency,
)
from src.evaluation.run_comparison import _blended_unit_cost, simulate
from src.trace_gen.generator import TraceConfig, generate_trace

logger = logging.getLogger("cloud.analyze_live")

COLOURS = {"baseline": "#64748b", "aware": "#dc2626"}  # the colours run_comparison already uses
ROLLING_MINUTES = 5


# --------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------


@dataclass
class LiveRun:
    run_id: str
    meta: dict[str, Any]
    results: pd.DataFrame
    decisions: pd.DataFrame
    driver: pd.DataFrame
    ticks: pd.DataFrame

    @property
    def policy(self) -> str:
        return str(self.meta["policy"])

    @property
    def sizing(self) -> str:
        return str(self.meta["sizing"])

    @property
    def time_scale(self) -> float:
        return float(self.meta["time_scale"])


def discover_runs(live_dir: Path) -> list[str]:
    return sorted(p.name.removesuffix("_meta.json") for p in live_dir.glob("*_meta.json"))


def load_run(run_id: str, live_dir: Path) -> LiveRun:
    meta = json.loads((live_dir / f"{run_id}_meta.json").read_text())
    return LiveRun(
        run_id=run_id, meta=meta,
        results=pd.read_csv(live_dir / f"{run_id}_results.csv"),
        decisions=pd.read_csv(live_dir / f"{run_id}_decisions.csv"),
        driver=pd.read_csv(live_dir / f"{run_id}_driver.csv"),
        ticks=pd.read_csv(live_dir / f"{run_id}_ticks.csv"),
    )


def trace_config(meta: Mapping[str, Any]) -> TraceConfig:
    """Rebuild the exact trace config the run used (rate fallback included)."""
    return replay.make_config(float(meta["base_rate_per_min"]))


# --------------------------------------------------------------------------------
# Live per-request latency
# --------------------------------------------------------------------------------


def live_per_request(run: LiveRun, minute_of: Mapping[int, int]) -> pd.DataFrame:
    """One row per completed request, in SIMULATED seconds (real seconds x K)."""
    k = run.time_scale
    df = run.results.copy()
    df["minute"] = df["request_id"].map(minute_of).astype(int)
    df["response_s"] = (df["end_ts"] - df["ingress_ts"]) * k
    df["wait_s"] = (df["start_ts"] - df["ingress_ts"]) * k
    df["sla_s"] = df["true_archetype"].map(SLA_SECONDS)
    df["sla_violated"] = df["response_s"] > df["sla_s"]
    cols = ["request_id", "minute", "true_archetype", "predicted_archetype", "pool", "wait_s", "response_s", "sla_s", "sla_violated"]
    return df[cols].sort_values("request_id").reset_index(drop=True)


# --------------------------------------------------------------------------------
# The same trace through the simulator, with the live capacity timeline
# --------------------------------------------------------------------------------


def decision_capacity_frame(decisions: pd.DataFrame, policy: str, delay_minutes: int = 0) -> pd.DataFrame:
    """The live controller's decisions in the per-minute frame ``capacity_timeline`` reads.

    ``delay_minutes=1`` shifts each decision to the minute after it was made, which is when a
    real controller's decision can first take effect.
    """
    rows: list[dict[str, Any]] = []
    for item in decisions.sort_values("minute").itertuples():
        capacity = json.loads(item.capacity)
        row: dict[str, Any] = {"minute": int(item.minute) + delay_minutes}
        if policy == "baseline":
            row["baseline_capacity"] = int(capacity[BASELINE_POOL])
        else:
            for pool in PRIMITIVE_MAP.values():
                row[f"aware_capacity_{pool}"] = int(capacity.get(pool, 0))
        rows.append(row)
    return pd.DataFrame(rows)


def labeled_with_live_predictions(cfg: TraceConfig, run: LiveRun, minutes: int) -> tuple[pd.DataFrame, int]:
    """The trace, truncated to the replayed minutes, with the LIVE router's predictions.

    Arrival offsets are drawn over the whole frame in request_id order, so it must be the
    unfiltered prefix of the trace. A request with no live result (a lost request) gets the
    seed-matched classifier's prediction so the stream stays aligned; the count is returned.
    """
    featured = add_proxy_features(generate_trace(cfg), seed=cfg.seed)
    featured = featured[featured["minute"] < minutes].reset_index(drop=True)
    live = run.results.set_index("request_id")["predicted_archetype"]
    local = classify(featured, train_classifier(add_proxy_features(generate_trace(cfg), seed=cfg.seed), seed=cfg.seed))
    featured["predicted_archetype"] = featured["request_id"].map(live)
    filled = int(featured["predicted_archetype"].isna().sum())
    featured["predicted_archetype"] = featured["predicted_archetype"].fillna(local["predicted_archetype"])
    return featured, filled


def sim_per_request(labeled: pd.DataFrame, run: LiveRun, delay_minutes: int) -> pd.DataFrame:
    policy: Literal["baseline", "aware"] = "aware" if run.policy == "aware" else "baseline"
    frame = decision_capacity_frame(run.decisions, policy, delay_minutes)
    return simulate_policy_latency(labeled, frame, policy, seed=ARRIVAL_SEED)


# --------------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------------


def _err(live: float, sim: float) -> dict[str, float]:
    return {"live": live, "sim": sim, "abs_err": live - sim, "rel_err": (live - sim) / sim if sim else math.nan}


def per_pool_comparison(live: pd.DataFrame, sim: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sim_by_id = sim.set_index("request_id")
    joined = live.assign(sim_response_s=live["request_id"].map(sim_by_id["response_s"]))
    for pool, group in joined.groupby("pool"):
        sim_pool = sim[sim["pool"] == pool]["response_s"]  # sim routes by the same live predictions
        out[str(pool)] = {
            "n_live": int(len(group)), "n_sim": int(len(sim_pool)),
            "p50": _err(float(group["response_s"].quantile(0.5)), float(sim_pool.quantile(0.5)) if len(sim_pool) else math.nan),
            "p95": _err(float(group["response_s"].quantile(0.95)), float(sim_pool.quantile(0.95)) if len(sim_pool) else math.nan),
        }
    return out


def sla_comparison(live_summary: Mapping[str, Any], sim_summary: Mapping[str, Any]) -> dict[str, Any]:
    out = {"overall": _err(live_summary["overall"]["sla_violation_rate"], sim_summary["overall"]["sla_violation_rate"])}
    for window in ("pre", "during", "post"):
        out[window] = _err(live_summary["by_window"][window]["sla_violation_rate"], sim_summary["by_window"][window]["sla_violation_rate"])
    return out


def paired_response_error(live: pd.DataFrame, sim: pd.DataFrame) -> dict[str, float]:
    """Per-request (live - sim) response, in simulated seconds, over requests present in both."""
    joined = live.merge(sim[["request_id", "response_s"]], on="request_id", suffixes=("", "_sim"))
    diff = joined["response_s"] - joined["response_s_sim"]
    return {"n": float(len(diff)), "median": float(diff.median()), "mean": float(diff.mean()), "p95": float(diff.quantile(0.95))}


def first_breach_minute(decisions: pd.DataFrame) -> int | None:
    breached = decisions[decisions["breach_projected"].astype(bool)]
    return None if breached.empty else int(breached["minute"].min())


def sim_first_breach_minute(results: pd.DataFrame, policy: str) -> int | None:
    """Simulated first breach under the LIVE definition: each policy governs its own spend.

    run_comparison projects the baseline from the aware policy's incurred spend because both share
    one simulated run; a live run is one policy, so the baseline's own spend is used instead
    (the aware projection is identical either way).
    """
    budget = float(EVALUATION_DEFAULTS["budget"])
    indexed = results.set_index("minute")
    if policy == "aware":
        breached = indexed["aware_breach_projected"]
    else:
        blended = _blended_unit_cost(int(EVALUATION_DEFAULTS["baseline_per_unit_capacity"]))
        duration = 120
        projection = indexed["baseline_actual_spend"] + indexed["baseline_capacity"] * blended * (duration - indexed.index.to_series())
        breached = projection.map(lambda spend: check_budget(float(spend), budget)["breach_projected"])
    hits = breached[breached.astype(bool)]
    return None if hits.empty else int(hits.index[0])


def capacity_timeline_frame(decisions: pd.DataFrame) -> pd.Series:
    """Total capacity units per minute from the live decisions."""
    return pd.Series(
        {int(r.minute): sum(json.loads(r.capacity).values()) for r in decisions.itertuples()}
    ).sort_index()


def run_counts(run: LiveRun) -> dict[str, Any]:
    ok = run.driver[(run.driver["status"] >= 200) & (run.driver["status"] < 300)]
    return {
        "scheduled": int(run.meta["n_scheduled"]), "sent": int(len(run.driver)), "accepted_2xx": int(len(ok)),
        "non_2xx": int(len(run.driver) - len(ok)), "retried": int((run.driver["attempts"] > 1).sum()),
        "results_rows": int(len(run.results)), "lost": int(len(ok) - len(run.results)),
        "aborted": bool(run.meta.get("aborted", False)), "drained": bool(run.meta.get("drained", True)),
        "ticks": int(len(run.ticks)), "tick_failures": int(((run.ticks["status"] != 200) | run.ticks["function_error"].notna()).sum()),
        "p99_lateness_s": float(run.driver["lateness_s"].quantile(0.99)) if len(run.driver) else math.nan,
        "tick_latency_s_p50": float(run.ticks["latency_s"].median()) if len(run.ticks) else math.nan,
    }


def analyse_run(run: LiveRun) -> dict[str, Any]:
    cfg = trace_config(run.meta)
    shift_end = cfg.shift_start_min + cfg.shift_duration_min
    minutes = int(run.meta["minutes"])
    schedule = replay.select_minutes(replay.build_schedule(cfg), minutes)
    minute_of = {int(r): int(m) for r, m in zip(schedule["request_id"], schedule["minute"])}

    live = live_per_request(run, minute_of)
    live_summary = summarise_latency(live, cfg.shift_start_min, shift_end)

    labeled, filled = labeled_with_live_predictions(cfg, run, minutes)
    sim_spec = sim_per_request(labeled, run, delay_minutes=0)
    sim_lag = sim_per_request(labeled, run, delay_minutes=1)
    present = set(live["request_id"])
    sim_spec_p, sim_lag_p = (s[s["request_id"].isin(present)] for s in (sim_spec, sim_lag))
    sim_spec_summary = summarise_latency(sim_spec_p, cfg.shift_start_min, shift_end)
    sim_lag_summary = summarise_latency(sim_lag_p, cfg.shift_start_min, shift_end)

    pure_sim, _ = simulate(cfg, sizing=run.sizing, recalibrate=False)
    return {
        "run_id": run.run_id, "policy": run.policy, "sizing": run.sizing, "time_scale": run.time_scale,
        "minutes": minutes, "base_rate_per_min": cfg.base_rate_per_min, "counts": run_counts(run),
        "predictions_filled_from_local_classifier": filled,
        "live": live_summary,
        "sim_spec_timeline": sim_spec_summary,
        "sim_decision_delayed_one_minute": sim_lag_summary,
        "sla_live_vs_sim_spec": sla_comparison(live_summary, sim_spec_summary),
        "sla_live_vs_sim_delayed": sla_comparison(live_summary, sim_lag_summary),
        "per_pool_live_vs_sim_spec": per_pool_comparison(live, sim_spec_p),
        "per_pool_live_vs_sim_delayed": per_pool_comparison(live, sim_lag_p),
        "paired_response_error_vs_sim_spec_s": paired_response_error(live, sim_spec_p),
        "paired_response_error_vs_sim_delayed_s": paired_response_error(live, sim_lag_p),
        "governance": {
            "first_breach_minute_live": first_breach_minute(run.decisions),
            "first_breach_minute_sim": sim_first_breach_minute(pure_sim, run.policy),
        },
        "capacity_units_total": {"live": capacity_timeline_frame(run.decisions).to_dict()},
        "_frames": {"live": live, "sim_spec": sim_spec_p, "sim_lag": sim_lag_p, "sim_capacity": pure_sim},
    }


def governance_lead_time(analyses: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """First breach minute per policy (live and sim) and the lead time, per sizing rule."""
    out: dict[str, Any] = {}
    for sizing in sorted({a["sizing"] for a in analyses}):
        by_policy = {a["policy"]: a["governance"] for a in analyses if a["sizing"] == sizing}
        if {"aware", "baseline"} <= set(by_policy):
            def lead(kind: str) -> int | None:
                b, a = by_policy["baseline"][kind], by_policy["aware"][kind]
                return None if b is None or a is None else b - a
            out[sizing] = {"live_lead_min": lead("first_breach_minute_live"), "sim_lead_min": lead("first_breach_minute_sim"),
                           "baseline": by_policy["baseline"], "aware": by_policy["aware"]}
    return out


# --------------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------------


def _plt() -> Any:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def plot_live_vs_sim(analyses: Sequence[Mapping[str, Any]], path: Path) -> None:
    plt = _plt()
    n = len(analyses)
    fig, axes = plt.subplots(2, n, figsize=(4.6 * n, 8), squeeze=False)
    for col, a in enumerate(analyses):
        ax = axes[0][col]
        pools = sorted(a["per_pool_live_vs_sim_spec"])
        x = np.arange(len(pools))
        sim = [a["per_pool_live_vs_sim_spec"][p]["p95"]["sim"] for p in pools]
        sim_d = [a["per_pool_live_vs_sim_delayed"][p]["p95"]["sim"] for p in pools]
        live = [a["per_pool_live_vs_sim_spec"][p]["p95"]["live"] for p in pools]
        ax.bar(x - 0.2, sim, 0.38, color="#cbd5e1", label="sim (spec timeline)")
        ax.bar(x + 0.2, sim_d, 0.38, color="#94a3b8", label="sim (decision delayed 1 min)")
        ax.plot(x, live, "o", color=COLOURS[a["policy"]], markersize=9, label="live")
        ax.set_xticks(x)
        ax.set_xticklabels(pools, rotation=30, ha="right", fontsize=7)
        ax.set_ylabel("p95 response (simulated s)")
        ax.set_title(f"{a['policy']} / {a['sizing']}  (K={a['time_scale']:g})", fontsize=10)
        ax.set_xlim(-0.7, max(len(pools), 2) - 0.3)  # keep a lone pool's bars a sensible width
        ax.set_ylim(0, 1.45 * max([*sim, *sim_d, *live, 1.0]))  # headroom so the legend never hides data
        ax.legend(fontsize=7, loc="upper left", framealpha=0.9)
        ax.grid(alpha=0.15, axis="y")

        ax2 = axes[1][col]
        live_cap = pd.Series(a["capacity_units_total"]["live"]).astype(float)
        live_cap.index = live_cap.index.astype(int)
        sim_res = a["_frames"]["sim_capacity"].set_index("minute")
        sim_cap = sim_res["baseline_capacity"] if a["policy"] == "baseline" else sim_res["aware_capacity_total"]
        ax2.step(live_cap.index, live_cap.values, where="post", color=COLOURS[a["policy"]], lw=2, label="live decisions")
        ax2.step(sim_cap.index, sim_cap.values, where="post", color="#0f172a", lw=1.2, ls="--", label="simulation")
        ax2.set_xlabel("simulated minute")
        ax2.set_ylabel("capacity units (total)")
        ax2.legend(fontsize=7)
        ax2.grid(alpha=0.15)
    fig.suptitle("Live vs simulation: per-pool p95 (top) and capacity timeline (bottom)", fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def rolling_violation_rate(per_request: pd.DataFrame, minutes: int) -> pd.Series:
    per_minute = per_request.groupby("minute")["sla_violated"].mean().reindex(range(minutes))
    return per_minute.rolling(ROLLING_MINUTES, min_periods=1).mean()


def plot_latency_timeline(analyses: Sequence[Mapping[str, Any]], path: Path) -> None:
    plt = _plt()
    sizings = sorted({a["sizing"] for a in analyses})
    fig, axes = plt.subplots(1, len(sizings), figsize=(6.5 * len(sizings), 4.5), squeeze=False)
    cfg = replay.make_config()
    for col, sizing in enumerate(sizings):
        ax = axes[0][col]
        for a in (x for x in analyses if x["sizing"] == sizing):
            series = rolling_violation_rate(a["_frames"]["live"], a["minutes"])
            ax.plot(series.index, series.values, color=COLOURS[a["policy"]], lw=1.8, label=f"{a['policy']} ({a['run_id'][-6:]})")
        ax.axvspan(cfg.shift_start_min, cfg.shift_start_min + cfg.shift_duration_min, color="#dc2626", alpha=0.07)
        ax.set_title(f"sizing={sizing}", fontsize=10)
        ax.set_xlabel("simulated minute")
        ax.set_ylabel(f"rolling {ROLLING_MINUTES}-min SLA-violation rate (live)")
        ax.set_ylim(-0.02, 1.02)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.15)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------


def _public(analysis: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in analysis.items() if not k.startswith("_")}


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Analyse live runs and validate the simulation against them")
    parser.add_argument("--live-dir", type=Path, default=replay.LIVE_DIR)
    parser.add_argument("--runs", nargs="*", help="run ids (default: every run with a _meta.json)")
    parser.add_argument("--include-aborted", action="store_true")
    args = parser.parse_args(argv)

    run_ids = args.runs or discover_runs(args.live_dir)
    runs = [load_run(r, args.live_dir) for r in run_ids]
    if not args.include_aborted:
        skipped = [r.run_id for r in runs if r.meta.get("aborted")]
        if skipped:
            logger.warning("skipping aborted runs: %s", skipped)
        runs = [r for r in runs if not r.meta.get("aborted")]
    if not runs:
        logger.error("no runs found in %s", args.live_dir)
        return 1

    analyses = [analyse_run(r) for r in runs]
    summary = {
        "runs": [_public(a) for a in analyses],
        "governance_lead_time": governance_lead_time(analyses),
    }
    args.live_dir.mkdir(parents=True, exist_ok=True)
    (args.live_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    plot_live_vs_sim(analyses, args.live_dir / "live_vs_sim.png")
    plot_latency_timeline(analyses, args.live_dir / "latency_timeline.png")
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
