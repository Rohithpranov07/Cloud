"""30-seed paired statistical evaluation — closes gap G2 (Addendum §10.1).

Every Part 1/Part 2 figure so far comes from one trace seed (42). PRD §8 asks for a
*statistically significant* difference, which a single seed cannot demonstrate. This
module runs both policies, under both sizing rules, over ``multiseed.seeds`` (30 trace
seeds by default) with the classifier's own randomness held fixed at
``multiseed.classifier_seed`` so only the trace varies, then tests baseline vs. aware on
three post-shift metrics with a paired Wilcoxon signed-rank test.

No test outcome is an acceptance gate (CLAUDE.md H3 / Addendum §10.1): whatever the
numbers are, they are reported.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import rankdata, wilcoxon

from src.config import DATA_DIR, MULTISEED_DEFAULTS
from src.evaluation.detection import first_detection_minute
from src.evaluation.latency_model import ARRIVAL_SEED, simulate_policy_latency, summarise_latency
from src.evaluation.run_comparison import POLICIES, SIZING_RULES, simulate
from src.trace_gen.generator import _default_config

RESULTS_CSV_PATH = DATA_DIR / "multiseed_results.csv"
SUMMARY_JSON_PATH = DATA_DIR / "multiseed_summary.json"
DISTRIBUTIONS_PLOT_PATH = DATA_DIR / "multiseed_distributions.png"

# The three Addendum §10.1 post-shift metrics the paired Wilcoxon test is run on.
METRICS: tuple[str, ...] = ("sla_violation_rate", "p95_response_s", "cost_per_compliant_request")

BOOTSTRAP_RESAMPLES: int = 10_000


def run_seed(seed: int, sizing: str) -> dict:
    """One row of per-seed metrics (Addendum §10.1): trace varies by ``seed``, the
    classifier stays fixed at ``multiseed.classifier_seed``.
    """
    classifier_seed = int(MULTISEED_DEFAULTS["classifier_seed"])
    cfg = replace(_default_config(), seed=seed)
    results, labeled = simulate(cfg, sizing=sizing, classifier_seed=classifier_seed)

    shift_end = cfg.shift_start_min + cfg.shift_duration_min
    indexed = results.set_index("minute")

    detect_minute = first_detection_minute(labeled, cfg)
    scaled_up = indexed[indexed["aware_capacity_eks_gpu_reserved"] >= 2]
    aware_scaleup_minute = float(scaled_up.index[0]) if not scaled_up.empty else float("nan")

    aware_breach = indexed[indexed["aware_breach_projected"]]
    baseline_breach = indexed[indexed["baseline_breach_projected"]]
    aware_breach_minute = float(aware_breach.index[0]) if not aware_breach.empty else float("nan")
    baseline_breach_minute = float(baseline_breach.index[0]) if not baseline_breach.empty else float("nan")
    governance_lead_min = (
        baseline_breach_minute - aware_breach_minute
        if not (np.isnan(baseline_breach_minute) or np.isnan(aware_breach_minute))
        else float("nan")
    )

    row: dict = {
        "seed": seed,
        "sizing": sizing,
        "detect_minute": float(detect_minute) if detect_minute is not None else float("nan"),
        "aware_scaleup_minute": aware_scaleup_minute,
        "aware_breach_minute": aware_breach_minute,
        "baseline_breach_minute": baseline_breach_minute,
        "governance_lead_min": governance_lead_min,
    }

    pre_post_cutoff = shift_end - 1
    for policy in POLICIES:
        per_request = simulate_policy_latency(labeled, results, policy, seed=ARRIVAL_SEED)
        summary = summarise_latency(per_request, cfg.shift_start_min, shift_end)
        post = summary["by_window"]["post"]

        spend_column = "baseline_actual_spend" if policy == "baseline" else "cumulative_actual_spend"
        spend_series = indexed[spend_column]
        spend_before = (
            float(spend_series.loc[pre_post_cutoff]) if pre_post_cutoff in spend_series.index else 0.0
        )
        spend_end = float(spend_series.iloc[-1])
        actual_spend_post = spend_end - spend_before

        post_requests = per_request[per_request["minute"] >= shift_end]
        n_within_sla = int((~post_requests["sla_violated"]).sum())
        cost_per_compliant_request = (
            actual_spend_post / n_within_sla if n_within_sla > 0 else float("nan")
        )

        row[f"{policy}_p95_response_s"] = post["p95_response_s"]
        row[f"{policy}_sla_violation_rate"] = post["sla_violation_rate"]
        row[f"{policy}_actual_spend"] = actual_spend_post
        row[f"{policy}_cost_per_compliant_request"] = cost_per_compliant_request

    return row


def _run_seed_task(task: tuple[int, str]) -> dict:
    seed, sizing = task
    return run_seed(seed, sizing)


def run_all(seeds: list[int], sizings: tuple[str, ...] = SIZING_RULES, workers: int = 1) -> pd.DataFrame:
    """Run every (seed, sizing) combination. Output is identical regardless of ``workers``."""
    tasks = [(seed, sizing) for sizing in sizings for seed in seeds]
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            rows = list(executor.map(_run_seed_task, tasks))
    else:
        rows = [run_seed(seed, sizing) for seed, sizing in tasks]
    return pd.DataFrame(rows)


def _rank_biserial(diff: np.ndarray) -> float:
    """Matched-pairs rank-biserial correlation: (W+ - W-) / (W+ + W-), zeros dropped
    (matching scipy's default ``zero_method="wilcox"``).
    """
    nonzero = diff[diff != 0]
    if len(nonzero) == 0:
        return float("nan")
    ranks = rankdata(np.abs(nonzero))
    positive = float(ranks[nonzero > 0].sum())
    negative = float(ranks[nonzero < 0].sum())
    total = positive + negative
    return (positive - negative) / total if total > 0 else float("nan")


def _bootstrap_median_ci(
    diff: np.ndarray, seed: int, n_resamples: int = BOOTSTRAP_RESAMPLES
) -> tuple[float, float]:
    """Seeded bootstrap 95% CI for the median paired difference."""
    rng = np.random.default_rng(seed)
    n = len(diff)
    resamples = rng.choice(diff, size=(n_resamples, n), replace=True)
    medians = np.median(resamples, axis=1)
    lower, upper = np.percentile(medians, [2.5, 97.5])
    return float(lower), float(upper)


def paired_tests(
    df: pd.DataFrame, bootstrap_seed: int = 0, n_resamples: int = BOOTSTRAP_RESAMPLES
) -> dict:
    """Addendum §10.1: for each sizing rule x metric, a paired two-sided Wilcoxon
    signed-rank test (baseline vs. aware), the median paired difference, its seeded
    bootstrap 95% CI, and the matched-pairs rank-biserial effect size.

    Checked against scipy 1.18.1's installed ``scipy.stats.wilcoxon`` signature via
    ``help()`` before writing this (Anti-Hallucination rule 1): ``zero_method="wilcox"``
    (the default) drops zero-difference pairs before ranking, which is also what
    ``_rank_biserial`` replicates. NaN pairs (a metric that never occurred for that seed)
    are dropped before the test; ``n`` records how many pairs were actually used.
    """
    summary: dict[str, dict[str, dict]] = {}
    for sizing in SIZING_RULES:
        summary[sizing] = {}
        subset = df[df["sizing"] == sizing]
        for metric in METRICS:
            baseline = subset[f"baseline_{metric}"].to_numpy(dtype=float)
            aware = subset[f"aware_{metric}"].to_numpy(dtype=float)
            valid = ~(np.isnan(baseline) | np.isnan(aware))
            b, a = baseline[valid], aware[valid]
            n = int(len(b))
            diff = a - b

            if n == 0 or bool(np.all(diff == 0)):
                p_value = float("nan")
                statistic = float("nan")
                rank_biserial_r = float("nan")
            else:
                test_result = wilcoxon(b, a, alternative="two-sided")
                p_value = float(test_result.pvalue)
                statistic = float(test_result.statistic)
                rank_biserial_r = _rank_biserial(diff)

            if n > 0:
                median_diff = float(np.median(diff))
                ci_low, ci_high = _bootstrap_median_ci(diff, seed=bootstrap_seed, n_resamples=n_resamples)
                median_baseline = float(np.median(b))
                median_aware = float(np.median(a))
            else:
                median_diff = ci_low = ci_high = median_baseline = median_aware = float("nan")

            summary[sizing][metric] = {
                "n": n,
                "median_baseline": median_baseline,
                "median_aware": median_aware,
                "median_paired_diff": median_diff,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "statistic": statistic,
                "p_value": p_value,
                "rank_biserial_r": rank_biserial_r,
            }
    return summary


def _plot_distributions(df: pd.DataFrame) -> None:
    """Write ``data/multiseed_distributions.png``: a paired dot-and-line plot per
    metric, one panel per sizing rule.
    """
    fig, axes = plt.subplots(len(METRICS), len(SIZING_RULES), figsize=(10, 13), squeeze=False)
    colours = {"baseline": "#64748b", "aware": "#dc2626"}

    for row, metric in enumerate(METRICS):
        for col, sizing in enumerate(SIZING_RULES):
            ax = axes[row][col]
            subset = df[df["sizing"] == sizing]
            baseline = subset[f"baseline_{metric}"].to_numpy(dtype=float)
            aware = subset[f"aware_{metric}"].to_numpy(dtype=float)
            valid = ~(np.isnan(baseline) | np.isnan(aware))
            baseline, aware = baseline[valid], aware[valid]

            for b_value, a_value in zip(baseline, aware):
                ax.plot([0, 1], [b_value, a_value], color="#cbd5e1", lw=0.8, zorder=1)
            ax.scatter(np.zeros(len(baseline)), baseline, color=colours["baseline"], zorder=2, s=22)
            ax.scatter(np.ones(len(aware)), aware, color=colours["aware"], zorder=2, s=22)

            ax.set_xticks([0, 1])
            ax.set_xticklabels(["baseline", "aware"])
            ax.set_xlim(-0.3, 1.3)
            ax.set_title(f"{metric}\nsizing={sizing} (n={len(baseline)})", fontsize=9)
            ax.grid(alpha=0.15)

    fig.suptitle("Paired per-seed metrics, baseline vs. aware (Addendum §10.1)", fontsize=13)
    fig.tight_layout()
    fig.savefig(DISTRIBUTIONS_PLOT_PATH, dpi=150)
    plt.close(fig)
    print(f"wrote {DISTRIBUTIONS_PLOT_PATH}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="30-seed paired statistical evaluation")
    parser.add_argument("--workers", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    seeds = [int(s) for s in MULTISEED_DEFAULTS["seeds"]]
    print(f"running {len(seeds)} seeds x {len(SIZING_RULES)} sizing rules, workers={args.workers}")

    df = run_all(seeds, SIZING_RULES, workers=args.workers)
    df.to_csv(RESULTS_CSV_PATH, index=False)
    print(f"wrote {RESULTS_CSV_PATH} ({len(df)} rows)")

    summary = paired_tests(df)
    with SUMMARY_JSON_PATH.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    print(f"wrote {SUMMARY_JSON_PATH}")
    print(json.dumps(summary, indent=2))

    _plot_distributions(df)


if __name__ == "__main__":
    main()
