"""Shared shift-detection helper — TRD §6 "Forecaster" gate, Addendum §10.1.

One definition of "has the per-archetype forecaster detected the compositional shift
yet", used by both ``tests/test_forecaster.py`` and ``src.evaluation.multiseed.run_seed``
(Anti-Hallucination rule 3: never a second definition of the same concept).
"""

from __future__ import annotations

import pandas as pd

from src.forecaster.per_archetype_forecaster import build_minute_series, forecast_all_archetypes
from src.trace_gen.generator import TraceConfig


def first_detection_minute(
    labeled: pd.DataFrame, cfg: TraceConfig, threshold: float = 0.25, horizon: int = 5
) -> int | None:
    """First minute >= ``cfg.shift_start_min`` at which the agentic forecast exceeds its
    pre-shift mean by more than ``threshold`` (Addendum §10.1 ``detect_minute``).

    Returns ``None`` if the shift is never detected within ``labeled``'s window.
    """
    pre_shift = labeled[labeled["minute"] < cfg.shift_start_min]
    agentic_baseline = float(
        build_minute_series(pre_shift, "predicted_archetype")["agentic_tool_using"].mean()
    )

    last_minute = int(labeled["minute"].max())
    for window_end in range(cfg.shift_start_min, last_minute + 1):
        window = labeled[labeled["minute"] <= window_end]
        agentic = float(forecast_all_archetypes(window, horizon=horizon)["agentic_tool_using"].mean())
        if agentic / agentic_baseline - 1.0 > threshold:
            return window_end
    return None
