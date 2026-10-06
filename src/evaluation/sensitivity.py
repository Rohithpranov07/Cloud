"""Sensitivity grid — Addendum §10.2: how large a composition shift must be before
archetype awareness pays for itself.

Sweeps the post-shift agentic share, the interactive-archetype SLA multiplier, and the
sizing rule, 10 seeds per cell. Reuses ``run_comparison.simulate`` and
``latency_model.simulate_policy_latency`` exactly as ``multiseed.run_seed`` does — just
with an overridden ``TraceConfig.mix_after`` and an overridden SLA multiplier applied
post-hoc to the already-simulated per-request latency, never by editing config on disk
or touching ``multiseed.py``/``latency_model.py`` (outside T10.5's files-you-may-touch).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from typing import Literal

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import TwoSlopeNorm

from src.config import ARCHETYPES, DATA_DIR, MULTISEED_DEFAULTS
from src.evaluation.latency_model import ARRIVAL_SEED, BASE_SERVICE_S, simulate_policy_latency
from src.evaluation.run_comparison import POLICIES, SIZING_RULES, simulate
from src.trace_gen.generator import TraceConfig, _default_config, generate_trace

SENSITIVITY_CSV_PATH = DATA_DIR / "sensitivity.csv"
SENSITIVITY_HEATMAP_PATH = DATA_DIR / "sensitivity_heatmap.png"

AGENTIC_SHARE_GRID: tuple[float, ...] = (0.20, 0.30, 0.40, 0.50)
SLA_MULTIPLIER_GRID: tuple[float, ...] = (2.0, 3.0, 5.0)
SEEDS_PER_CELL: int = 10
# Addendum §10.2: the SLA multiplier sweep applies to the "interactive" archetypes only
# — batch_offline is explicitly latency-insensitive (its default multiplier is 10, not
# 3) and stays fixed at its own default throughout the grid.
INTERACTIVE_ARCHETYPES: tuple[str, ...] = tuple(a for a in ARCHETYPES if a != "batch_offline")

FLAT_AGGREGATE_MAX_CV: float = 0.3  # TRD §1.3 invariant: std/mean < 0.3


def _mix_after_for_agentic_share(agentic_share: float) -> dict[str, float]:
    """Move share between short_conversational and agentic_tool_using only; everything
    else stays at the default mix_after (Addendum §10.2).
    """
    default_mix = dict(_default_config().mix_after or {})
    delta = default_mix["agentic_tool_using"] - agentic_share
    mix = dict(default_mix)
    mix["agentic_tool_using"] = agentic_share
    mix["short_conversational"] = default_mix["short_conversational"] + delta
    return mix


def _assert_mix_is_valid(cfg: TraceConfig) -> None:
    """Addendum §10.2: assert the mix sums to 1 and the flat-aggregate invariant holds
    for every variant BEFORE running it.
    """
    mix = cfg.mix_after or {}
    total = sum(mix.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"mix_after does not sum to 1.0 (got {total}): {mix}")

    trace = generate_trace(cfg)
    per_minute = trace.groupby("minute").size()
    cv = float(per_minute.std() / per_minute.mean())
    if cv >= FLAT_AGGREGATE_MAX_CV:
        raise ValueError(
            f"flat-aggregate invariant violated for mix_after={mix}: std/mean={cv:.4f} >= {FLAT_AGGREGATE_MAX_CV}"
        )


def _post_shift_sla_violation_rate(
    labeled: pd.DataFrame,
    results: pd.DataFrame,
    policy: Literal["baseline", "aware"],
    cfg: TraceConfig,
    sla_multiplier: float,
) -> float:
    """Post-shift SLA-violation rate for one policy, with the interactive-archetype SLA
    multiplier overridden (batch_offline keeps its own default multiplier).
    """
    per_request = simulate_policy_latency(labeled, results, policy, seed=ARRIVAL_SEED)

    # batch_offline is excluded from the sweep (Addendum §10.2): it keeps its own
    # default multiplier (10), since it is explicitly latency-insensitive.
    multiplier_by_archetype = {a: sla_multiplier for a in INTERACTIVE_ARCHETYPES}
    multiplier_by_archetype["batch_offline"] = 10.0
    sla_seconds = {a: multiplier_by_archetype[a] * BASE_SERVICE_S[a] for a in ARCHETYPES}

    sla_s = per_request["true_archetype"].map(sla_seconds)
    sla_violated = per_request["response_s"] > sla_s

    shift_end = cfg.shift_start_min + cfg.shift_duration_min
    post = sla_violated[per_request["minute"] >= shift_end]
    if len(post) == 0:
        return float("nan")
    return float(post.mean())


def _run_cell_seed(seed: int, sizing: str, agentic_share: float, sla_multiplier: float) -> dict:
    classifier_seed = int(MULTISEED_DEFAULTS["classifier_seed"])
    mix_after = _mix_after_for_agentic_share(agentic_share)
    cfg = replace(_default_config(), seed=seed, mix_after=mix_after)
    _assert_mix_is_valid(cfg)

    results, labeled = simulate(cfg, sizing=sizing, classifier_seed=classifier_seed)

    rates = {
        policy: _post_shift_sla_violation_rate(labeled, results, policy, cfg, sla_multiplier)
        for policy in POLICIES
    }
    return {
        "seed": seed,
        "sizing": sizing,
        "agentic_share": agentic_share,
        "sla_multiplier": sla_multiplier,
        "baseline_sla_violation_rate": rates["baseline"],
        "aware_sla_violation_rate": rates["aware"],
        "difference_baseline_minus_aware": rates["baseline"] - rates["aware"],
    }


def _run_cell_seed_task(task: tuple[int, str, float, float]) -> dict:
    return _run_cell_seed(*task)


def run_grid(workers: int = 1) -> pd.DataFrame:
    """Run the full grid: 4 agentic shares x 3 SLA multipliers x 2 sizing rules x
    10 seeds = 240 simulate() calls.
    """
    tasks = [
        (seed, sizing, agentic_share, sla_multiplier)
        for sizing in SIZING_RULES
        for agentic_share in AGENTIC_SHARE_GRID
        for sla_multiplier in SLA_MULTIPLIER_GRID
        for seed in range(SEEDS_PER_CELL)
    ]
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            rows = list(executor.map(_run_cell_seed_task, tasks))
    else:
        rows = [_run_cell_seed(*task) for task in tasks]
    return pd.DataFrame(rows)


def _heatmap_table(df: pd.DataFrame, sizing: str) -> pd.DataFrame:
    """Median (baseline - aware) SLA-violation-rate difference, agentic share x SLA multiplier."""
    subset = df[df["sizing"] == sizing]
    pivot = subset.pivot_table(
        index="sla_multiplier", columns="agentic_share", values="difference_baseline_minus_aware",
        aggfunc="median",
    )
    return pivot.reindex(index=SLA_MULTIPLIER_GRID, columns=AGENTIC_SHARE_GRID)


def _plot_heatmap(df: pd.DataFrame) -> None:
    """Write data/sensitivity_heatmap.png: one heatmap per sizing rule, diverging palette
    (grey at 0), annotated cells.
    """
    fig, axes = plt.subplots(1, len(SIZING_RULES), figsize=(12, 5), squeeze=False)
    norm = TwoSlopeNorm(vmin=-1.0, vcenter=0.0, vmax=1.0)

    for col, sizing in enumerate(SIZING_RULES):
        ax = axes[0][col]
        table = _heatmap_table(df, sizing)
        grid = table.to_numpy(dtype=float)
        image = ax.imshow(grid, cmap="RdGy_r", norm=norm, aspect="auto")

        ax.set_xticks(range(len(AGENTIC_SHARE_GRID)))
        ax.set_xticklabels([f"{s:.0%}" for s in AGENTIC_SHARE_GRID])
        ax.set_yticks(range(len(SLA_MULTIPLIER_GRID)))
        ax.set_yticklabels([f"{m:g}x" for m in SLA_MULTIPLIER_GRID])
        ax.set_xlabel("post-shift agentic share")
        ax.set_ylabel("SLA multiplier (interactive archetypes)")
        ax.set_title(f"sizing={sizing}", fontsize=11)

        for i in range(grid.shape[0]):
            for j in range(grid.shape[1]):
                value = grid[i, j]
                text = "n/a" if np.isnan(value) else f"{value:.2f}"
                ax.text(j, i, text, ha="center", va="center", fontsize=9)

        fig.colorbar(image, ax=ax, label="baseline - aware SLA-violation rate")

    fig.suptitle(
        "Sensitivity: when does archetype awareness pay off? (Addendum §10.2)", fontsize=13
    )
    fig.tight_layout()
    fig.savefig(SENSITIVITY_HEATMAP_PATH, dpi=150)
    plt.close(fig)
    print(f"wrote {SENSITIVITY_HEATMAP_PATH}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sensitivity grid (Addendum §10.2)")
    parser.add_argument("--workers", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    total_cells = len(AGENTIC_SHARE_GRID) * len(SLA_MULTIPLIER_GRID) * len(SIZING_RULES)
    print(
        f"running {total_cells} cells x {SEEDS_PER_CELL} seeds = "
        f"{total_cells * SEEDS_PER_CELL} runs, workers={args.workers}"
    )

    df = run_grid(workers=args.workers)
    df.to_csv(SENSITIVITY_CSV_PATH, index=False)
    print(f"wrote {SENSITIVITY_CSV_PATH} ({len(df)} rows)")

    for sizing in SIZING_RULES:
        print(f"\n[sizing={sizing}] median (baseline - aware) SLA-violation-rate difference:")
        print(_heatmap_table(df, sizing).to_string())

    _plot_heatmap(df)


if __name__ == "__main__":
    main()
