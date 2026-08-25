"""End-to-end head-to-head evaluation — TRD §1.8, §2 and the §6 "Evaluation" gate.

Runs the archetype-aware pipeline and the archetype-agnostic baseline over the SAME
generated trace, minute by minute, with independent capacity state, and records what each
one decided. This is the project's headline result: it answers whether knowing *what kind*
of request is coming, not just *how many*, makes autoscaling and cost governance
measurably better.

The comparison isolates the value of archetype information by construction. The aware
policy receives a signal the baseline does not, and that is the entire point of the
experiment rather than a flaw in it (PRD §9). Everything else — the trace, the rounding
rule, the sizing rule, the forecast horizon, the cost table — is held identical.

Outputs:
- ``data/evaluation_results.csv``  — one EvaluationRow per minute (TRD §1.8)
- ``data/capacity_comparison.png`` — baseline vs. aware capacity over time

Every Phase 1-6 function is imported and called, never reimplemented.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # headless: the evaluation writes files, it never opens a window

import matplotlib.pyplot as plt
import pandas as pd
from sklearn.metrics import accuracy_score

from src.classifier.archetype_classifier import add_proxy_features, classify, train_classifier
from src.config import (
    ARCHETYPES,
    DATA_DIR,
    EVALUATION_DEFAULTS,
    PRIMITIVE_MAP,
    PRIMITIVE_UNIT_CAPACITY,
    TRACE_DEFAULTS,
    UNIT_COST_PER_MIN,
)
from src.controller.archetype_aware_policy import archetype_aware_scaling_decision
from src.controller.baseline_policy import BASELINE_POOL, baseline_scaling_decision
from src.cost_governance.budget_projector import check_budget, project_spend
from src.feedback.recalibration import recalibrate
from src.forecaster.per_archetype_forecaster import forecast_aggregate, forecast_all_archetypes
from src.trace_gen.generator import TraceConfig, _default_config, generate_trace

RESULTS_PATH = DATA_DIR / "evaluation_results.csv"
PLOT_PATH = DATA_DIR / "capacity_comparison.png"

# --- How the two cost projections are made comparable ---------------------------------
#
# Both policies govern the SAME real infrastructure. What differs is the cost model each
# one can build, which is the whole claim under test:
#
#   archetype-aware  — projects from the per-archetype forecast, so it prices each
#                      primitive at its true rate and sees demand move toward the
#                      expensive one (eks_gpu_reserved, 0.09/unit/min).
#   aggregate-only   — sees one homogeneous pool and one flat request rate, so it can
#                      only price that pool at a single BLENDED rate calibrated to the
#                      workload mix it has observed. When composition shifts toward
#                      expensive primitives at constant request rate, its projection does
#                      not move, because neither of its two inputs moved.
#
# The blended rate is derived, not invented: for each archetype, cost per served
# request/minute is UNIT_COST_PER_MIN / PRIMITIVE_UNIT_CAPACITY. Weighting those by the
# pre-shift archetype mix and multiplying by the baseline's own per-unit throughput gives
# the price of one homogeneous unit. Calibrating on the PRE-shift mix is the point: it is
# the best rate an aggregate-only view could possibly have, and it goes stale precisely
# because composition changed underneath it.


def _blended_unit_cost(per_unit_capacity: int) -> float:
    """Price one baseline homogeneous unit at the pre-shift workload's blended rate."""
    mix = TRACE_DEFAULTS["mix_before"]
    cost_per_request = sum(
        float(mix[archetype])
        * UNIT_COST_PER_MIN[PRIMITIVE_MAP[archetype]]
        / PRIMITIVE_UNIT_CAPACITY[PRIMITIVE_MAP[archetype]]
        for archetype in ARCHETYPES
    )
    return cost_per_request * per_unit_capacity


def run() -> None:
    """Run both policies over one trace and write the two evaluation artifacts."""
    cfg: TraceConfig = _default_config()
    start_minute = int(EVALUATION_DEFAULTS["start_minute"])
    horizon = int(EVALUATION_DEFAULTS["forecast_horizon"])
    per_unit_capacity = int(EVALUATION_DEFAULTS["baseline_per_unit_capacity"])
    recalibrate_every = int(EVALUATION_DEFAULTS["recalibrate_every_minutes"])
    budget = float(EVALUATION_DEFAULTS["budget"])

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # --- Phase 1-2: trace, proxy features, classifier -----------------------------
    print("=" * 78)
    print("SEMANTIC AUTOSCALING — ARCHETYPE-AWARE vs. ARCHETYPE-AGNOSTIC")
    print("=" * 78)
    print(f"\ntrace: {cfg.duration_minutes} min @ {cfg.base_rate_per_min} req/min, "
          f"shift at minute {cfg.shift_start_min} over {cfg.shift_duration_min} min, seed {cfg.seed}")

    trace = generate_trace(cfg)
    featured = add_proxy_features(trace, seed=cfg.seed)
    clf = train_classifier(featured, seed=cfg.seed)
    labeled = classify(featured, clf)
    print(f"generated {len(labeled)} requests\n")

    # --- Phase 3-6: minute-by-minute head-to-head ---------------------------------
    baseline_capacity = 1
    aware_capacity: dict[str, int] = {}
    rows: list[dict] = []
    cumulative_actual_spend = 0.0
    blended_unit_cost = _blended_unit_cost(per_unit_capacity)

    print(f"recalibrating every {recalibrate_every} minutes")
    print(f"budget {budget} for the {cfg.duration_minutes}-minute window; "
          f"baseline blended unit cost {_blended_unit_cost(per_unit_capacity):.5f}/min")
    print("-" * 78)

    for minute in range(start_minute, cfg.duration_minutes):
        window = labeled[labeled["minute"] <= minute]
        minutes_remaining = cfg.duration_minutes - minute

        # Phase 6: recalibrate on an expanding window, logging before/after accuracy.
        if minute > start_minute and (minute - start_minute) % recalibrate_every == 0:
            accuracy_before = float(
                accuracy_score(window["true_archetype"], classify(window, clf)["predicted_archetype"])
            )
            clf = recalibrate(window, seed=cfg.seed)
            accuracy_after = float(
                accuracy_score(window["true_archetype"], classify(window, clf)["predicted_archetype"])
            )
            labeled = classify(featured, clf)
            direction = "improved" if accuracy_after >= accuracy_before else "REGRESSED"
            print(f"  minute {minute:>3}: recalibrated on {len(window):>5} rows — "
                  f"accuracy {accuracy_before:.4f} -> {accuracy_after:.4f} ({direction})")

        # Phase 3-4: forecast, then decide, independently for each policy.
        baseline_capacity = baseline_scaling_decision(
            forecast_aggregate(window, horizon), baseline_capacity, per_unit_capacity
        )["new_capacity"]

        for decision in archetype_aware_scaling_decision(
            forecast_all_archetypes(window, horizon), aware_capacity
        ):
            aware_capacity[decision["target_pool"]] = decision["new_capacity"]

        # Phase 5: project END-OF-WINDOW spend and check it against the window's budget.
        # Real budget governance (and PRD §4) asks whether the run is on course to breach
        # by the end of the billing window, so the figure compared against the budget is
        # spend already incurred plus spend projected over the minutes remaining.
        cumulative_actual_spend += project_spend(aware_capacity, 1)

        aware_spend = cumulative_actual_spend + project_spend(aware_capacity, minutes_remaining)
        baseline_spend = cumulative_actual_spend + (
            baseline_capacity * blended_unit_cost * minutes_remaining
        )
        baseline_budget = check_budget(baseline_spend, budget)
        aware_budget = check_budget(aware_spend, budget)

        row: dict = {
            # --- the five required TRD §1.8 EvaluationRow columns ---
            "minute": minute,
            "baseline_capacity": int(baseline_capacity),
            "aware_capacity_total": int(sum(aware_capacity.values())),
            "baseline_projected_spend": baseline_spend,
            "aware_projected_spend": aware_spend,
            "cumulative_actual_spend": cumulative_actual_spend,
            # --- additional columns (permitted by TRD §1.8) ---
            "baseline_breach_projected": baseline_budget["breach_projected"],
            "aware_breach_projected": aware_budget["breach_projected"],
            "baseline_budget_action": baseline_budget["action"],
            "aware_budget_action": aware_budget["action"],
        }
        for primitive in PRIMITIVE_MAP.values():
            row[f"aware_capacity_{primitive}"] = int(aware_capacity.get(primitive, 0))
        rows.append(row)

    results = pd.DataFrame(rows)
    results.to_csv(RESULTS_PATH, index=False)
    print("-" * 78)
    print(f"\nwrote {RESULTS_PATH} ({len(results)} rows)")

    _plot(results, cfg, budget)
    _summarise(results, cfg)


def _plot(results: pd.DataFrame, cfg: TraceConfig, budget: float) -> None:
    """Write ``data/capacity_comparison.png``: baseline vs. aware capacity over time."""
    shift_end = cfg.shift_start_min + cfg.shift_duration_min
    fig, (ax_capacity, ax_spend) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [3, 2]}
    )

    ax_capacity.plot(
        results["minute"], results["baseline_capacity"],
        color="#64748b", lw=2.2, label="baseline (archetype-agnostic)",
    )
    ax_capacity.plot(
        results["minute"], results["aware_capacity_total"],
        color="#dc2626", lw=2.2, label="archetype-aware (total units)",
    )
    ax_capacity.axvline(cfg.shift_start_min, color="#dc2626", ls="--", lw=1.4)
    ax_capacity.axvspan(cfg.shift_start_min, shift_end, color="#dc2626", alpha=0.07)
    ax_capacity.annotate(
        "compositional shift\n(aggregate rate unchanged)",
        xy=(shift_end + 2, ax_capacity.get_ylim()[1] * 0.62),
        fontsize=9, color="#dc2626",
    )
    ax_capacity.set_ylabel("capacity units")
    ax_capacity.set_title(
        "Capacity response to a compositional shift at constant aggregate request rate",
        fontsize=13, pad=12,
    )
    ax_capacity.legend(loc="upper left", fontsize=9)
    ax_capacity.grid(alpha=0.15)

    ax_spend.plot(
        results["minute"], results["baseline_projected_spend"],
        color="#64748b", lw=2, label="baseline projected spend",
    )
    ax_spend.plot(
        results["minute"], results["aware_projected_spend"],
        color="#dc2626", lw=2, label="archetype-aware projected spend",
    )
    ax_spend.axhline(budget, color="#0f172a", ls="-.", lw=1.6, label=f"budget = {budget:g}")
    ax_spend.axvline(cfg.shift_start_min, color="#dc2626", ls="--", lw=1.4)
    ax_spend.axvspan(cfg.shift_start_min, shift_end, color="#dc2626", alpha=0.07)

    # Mark where each policy first projects a breach — the lead-time result (PRD §8).
    for column, colour, label in (
        ("aware_breach_projected", "#dc2626", "aware detects"),
        ("baseline_breach_projected", "#64748b", "aggregate-only detects"),
    ):
        breaching = results[results[column]]
        if breaching.empty:
            continue
        first_minute = int(breaching["minute"].iloc[0])
        ax_spend.plot([first_minute], [budget], marker="v", markersize=11, color=colour, zorder=5)
        ax_spend.annotate(
            f"{label}\nminute {first_minute}",
            xy=(first_minute, budget),
            xytext=(first_minute - 2, budget - (budget * 0.07)),
            fontsize=8.5, color=colour, ha="right",
        )

    ax_spend.set_xlabel("simulation minute")
    ax_spend.set_ylabel("projected end-of-window spend")
    ax_spend.legend(loc="upper left", fontsize=9)
    ax_spend.grid(alpha=0.15)

    fig.tight_layout()
    fig.savefig(PLOT_PATH, dpi=150)
    plt.close(fig)
    print(f"wrote {PLOT_PATH}")


def _summarise(results: pd.DataFrame, cfg: TraceConfig) -> None:
    """Print the headline comparison the report's Evaluation section cites."""
    indexed = results.set_index("minute")
    pre = indexed.loc[: cfg.shift_start_min - 1]
    post = indexed.loc[cfg.shift_start_min :]

    baseline_pre = float(pre["baseline_capacity"].mean())
    baseline_post = float(post["baseline_capacity"].mean())
    aware_pre = float(pre["aware_capacity_total"].mean())
    aware_post = float(post["aware_capacity_total"].mean())

    print("\n" + "=" * 78)
    print("HEADLINE RESULT")
    print("=" * 78)
    print(f"\n{'':>22} {'pre-shift':>12} {'post-shift':>12} {'change':>12}")
    print(f"{'baseline capacity':>22} {baseline_pre:>12.2f} {baseline_post:>12.2f} "
          f"{(baseline_post / baseline_pre - 1) * 100:>11.1f}%")
    print(f"{'aware capacity':>22} {aware_pre:>12.2f} {aware_post:>12.2f} "
          f"{(aware_post / aware_pre - 1) * 100:>11.1f}%")

    normalised_gap = abs(aware_post / aware_pre - baseline_post / baseline_pre)
    print(f"\nnormalised capacity divergence after the shift: {normalised_gap:.4f}")

    print("\nfinal per-primitive allocation (archetype-aware):")
    for archetype in ARCHETYPES:
        primitive = PRIMITIVE_MAP[archetype]
        column = f"aware_capacity_{primitive}"
        print(f"  {archetype:>22} -> {primitive:<20} "
              f"pre {pre[column].mean():5.2f} -> post {post[column].mean():5.2f} units")

    baseline_breach = indexed[indexed["baseline_breach_projected"]]
    aware_breach = indexed[indexed["aware_breach_projected"]]
    print("\nbudget governance:")
    if not aware_breach.empty:
        first = int(aware_breach.index[0])
        print(f"  archetype-aware first projects a breach at minute {first} "
              f"(action: {aware_breach.iloc[0]['aware_budget_action']})")
    else:
        print("  archetype-aware never projects a breach")
    if not baseline_breach.empty:
        first_baseline = int(baseline_breach.index[0])
        print(f"  aggregate-only first projects a breach at minute {first_baseline}")
        if not aware_breach.empty:
            lead = first_baseline - int(aware_breach.index[0])
            print(f"  detection lead time for the archetype-aware layer: {lead} minutes")
    else:
        print("  aggregate-only NEVER projects a breach — it cannot see the cost shift at all")


if __name__ == "__main__":
    run()
