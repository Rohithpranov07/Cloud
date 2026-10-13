"""Adapters between live AWS data shapes and the unchanged ``src`` functions (§11.4).

The controller only has per-minute counters (``semauto-counts``), but the forecaster
consumes a request-level frame. Expanding counts back into one row per request makes
``build_minute_series`` see exactly the same per-minute series it would have seen on
the full trace, so the live forecast equals the simulated one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from src.config import ARCHETYPES


def count_item_from_dynamodb(item: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """One low-level ``semauto-counts`` item → ``{"minute": m, <archetype>: n, ...}``."""
    plain = {"minute": int(item["minute"]["N"])}
    for archetype in ARCHETYPES:
        if archetype in item:
            plain[archetype] = int(item[archetype]["N"])
    return plain


def counts_to_labeled(items: Iterable[Mapping[str, int]]) -> pd.DataFrame:
    """Expand per-minute counter items into the minimal request-level frame.

    Columns: ``minute`` and ``predicted_archetype`` — all that ``forecast_all_archetypes``
    and ``forecast_aggregate`` read. Minutes with no counts get no rows; the forecaster's
    zero-fill turns them into explicit zeros.
    """
    minutes: list[int] = []
    archetypes: list[str] = []
    counts: list[int] = []
    for item in items:
        minute = int(item["minute"])
        for archetype in ARCHETYPES:
            count = int(item.get(archetype, 0))
            if count < 0:
                raise ValueError(f"negative count {count} for {archetype} at minute {minute}")
            if count:
                minutes.append(minute)
                archetypes.append(archetype)
                counts.append(count)

    return pd.DataFrame(
        {
            "minute": np.repeat(np.array(minutes, dtype=np.int64), counts),
            "predicted_archetype": np.repeat(np.array(archetypes, dtype=object), counts),
        }
    )
