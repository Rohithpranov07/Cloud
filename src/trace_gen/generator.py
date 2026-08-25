"""Synthetic request-trace generator — TRD §1.2 (Request schema) and §1.3 (TraceConfig).

Produces a labeled request stream across the four fixed archetypes with a
*compositional shift*: the mix of archetypes changes while the aggregate
requests/minute rate stays flat. That invariant (TRD §1.3) is the entire
experimental premise — the shift must be invisible in the aggregate signal and
visible only in the per-archetype composition.

This module generates ground-truth data only. Proxy features and predicted
labels are added downstream by ``src.classifier`` (TRD §1.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.config import ARCHETYPES, DATA_DIR, TRACE_DEFAULTS

# Simulated compute units consumed by one request of each archetype, before noise.
# Ordering is what matters and it follows the workload semantics: a short chat turn is
# cheapest; a long-context RAG call pays for a large prompt; an agentic request pays for
# several tool-calling turns and is the most expensive; a batch job is moderate but
# latency-insensitive. Gaussian noise at COMPUTE_COST_NOISE_FRAC of the base is applied.
COMPUTE_COST_BASE: dict[str, float] = {
    "short_conversational": 1.0,
    "long_context_rag": 4.0,
    "agentic_tool_using": 8.0,
    "batch_offline": 2.5,
}
COMPUTE_COST_NOISE_FRAC: float = 0.15
COMPUTE_COST_FLOOR: float = 0.01  # keeps the Gaussian tail from producing <= 0 cost

N_TENANTS: int = 5  # TRD §1.2: tenant_id is 0-4 in the Core Prototype


@dataclass
class TraceConfig:
    """Trace generation parameters — field names, types and defaults per TRD §1.3."""

    duration_minutes: int = 120
    base_rate_per_min: float = 50.0
    shift_start_min: int = 60
    shift_duration_min: int = 15
    mix_before: dict | None = None  # archetype -> proportion, sums to 1.0
    mix_after: dict | None = None
    seed: int = 42


def _default_config() -> TraceConfig:
    """Build a TraceConfig from ``configs/default.yaml`` (values tunable, keys fixed)."""
    defaults: dict[str, Any] = TRACE_DEFAULTS
    return TraceConfig(
        duration_minutes=int(defaults["duration_minutes"]),
        base_rate_per_min=float(defaults["base_rate_per_min"]),
        shift_start_min=int(defaults["shift_start_min"]),
        shift_duration_min=int(defaults["shift_duration_min"]),
        mix_before=dict(defaults["mix_before"]),
        mix_after=dict(defaults["mix_after"]),
        seed=int(defaults["seed"]),
    )


def _resolve_mixes(cfg: TraceConfig) -> tuple[np.ndarray, np.ndarray]:
    """Return (mix_before, mix_after) as probability vectors ordered like ARCHETYPES."""
    defaults = TRACE_DEFAULTS
    before = cfg.mix_before if cfg.mix_before is not None else defaults["mix_before"]
    after = cfg.mix_after if cfg.mix_after is not None else defaults["mix_after"]

    vectors: list[np.ndarray] = []
    for name, mix in (("mix_before", before), ("mix_after", after)):
        if set(mix) != set(ARCHETYPES):
            raise ValueError(f"{name} must have exactly the four archetypes as keys, got {sorted(mix)}")
        vector = np.array([float(mix[a]) for a in ARCHETYPES], dtype=float)
        if np.any(vector < 0.0):
            raise ValueError(f"{name} proportions must be non-negative")
        if abs(vector.sum() - 1.0) > 1e-9:
            raise ValueError(f"{name} proportions must sum to 1.0, got {vector.sum()}")
        vectors.append(vector)
    return vectors[0], vectors[1]


def _mix_at_minute(
    minute: int, mix_before: np.ndarray, mix_after: np.ndarray, cfg: TraceConfig
) -> np.ndarray:
    """Linearly interpolate the archetype mix across the shift window (TRD §1.3).

    Before ``shift_start_min`` the mix is ``mix_before``; at or after
    ``shift_start_min + shift_duration_min`` it is ``mix_after``; in between it ramps
    linearly. Note the ramp is over the half-open interval
    ``[shift_start_min, shift_start_min + shift_duration_min)``.
    """
    if minute < cfg.shift_start_min:
        return mix_before
    shift_end = cfg.shift_start_min + cfg.shift_duration_min
    if minute >= shift_end:
        return mix_after
    if cfg.shift_duration_min <= 0:
        return mix_after
    progress = (minute - cfg.shift_start_min) / cfg.shift_duration_min
    return (1.0 - progress) * mix_before + progress * mix_after


def generate_trace(cfg: TraceConfig | None = None) -> pd.DataFrame:
    """Generate a labeled request trace conforming to the TRD §1.2 Request schema.

    The per-minute request COUNT is Poisson(``base_rate_per_min``) and is constant in
    expectation across the whole duration — the shift changes only which archetype each
    request is drawn from, never how many arrive (TRD §1.3 invariant).

    Fully seeded via ``np.random.default_rng(cfg.seed)``; no global numpy random state
    is touched, so two runs with the same seed are identical (NFR-2).
    """
    if cfg is None:
        cfg = _default_config()
    if cfg.duration_minutes <= 0:
        raise ValueError(f"duration_minutes must be positive, got {cfg.duration_minutes}")
    if cfg.base_rate_per_min <= 0:
        raise ValueError(f"base_rate_per_min must be positive, got {cfg.base_rate_per_min}")

    mix_before, mix_after = _resolve_mixes(cfg)
    rng = np.random.default_rng(cfg.seed)

    minutes: list[int] = []
    archetypes: list[str] = []

    for minute in range(cfg.duration_minutes):
        n_requests = int(rng.poisson(cfg.base_rate_per_min))
        if n_requests == 0:
            continue
        mix = _mix_at_minute(minute, mix_before, mix_after, cfg)
        drawn = rng.choice(len(ARCHETYPES), size=n_requests, p=mix)
        minutes.extend([minute] * n_requests)
        archetypes.extend(ARCHETYPES[i] for i in drawn)

    n_total = len(minutes)
    base_costs = np.array([COMPUTE_COST_BASE[a] for a in archetypes], dtype=float)
    noise = rng.normal(loc=0.0, scale=COMPUTE_COST_NOISE_FRAC, size=n_total)
    compute_cost = np.maximum(base_costs * (1.0 + noise), COMPUTE_COST_FLOOR)

    return pd.DataFrame(
        {
            "request_id": np.arange(n_total, dtype=int),
            "minute": np.array(minutes, dtype=int),
            "true_archetype": archetypes,
            "compute_cost": compute_cost,
            "tenant_id": rng.integers(0, N_TENANTS, size=n_total),
        }
    )


def main() -> None:
    """Write ``data/trace.csv`` and print a per-minute sanity check."""
    cfg = _default_config()
    df = generate_trace(cfg)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "trace.csv"
    df.to_csv(out_path, index=False)

    per_minute = df.groupby("minute").size()
    ratio = float(per_minute.std() / per_minute.mean())
    print(f"wrote {out_path} ({len(df)} requests over {cfg.duration_minutes} minutes)")
    print(f"per-minute count: mean={per_minute.mean():.2f} std={per_minute.std():.2f} std/mean={ratio:.4f}")
    print(f"flat-aggregate invariant (TRD §1.3, std/mean < 0.3): {'PASS' if ratio < 0.3 else 'FAIL'}")

    shift_end = cfg.shift_start_min + cfg.shift_duration_min
    for label, lo, hi in (("pre-shift", 0, cfg.shift_start_min), ("post-shift", shift_end, cfg.duration_minutes)):
        window = df[(df["minute"] >= lo) & (df["minute"] < hi)]
        shares = window["true_archetype"].value_counts(normalize=True)
        formatted = ", ".join(f"{a}={shares.get(a, 0.0):.3f}" for a in ARCHETYPES)
        print(f"{label:>10} mix: {formatted}")


if __name__ == "__main__":
    main()
