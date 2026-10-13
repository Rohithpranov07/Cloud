"""Shared plumbing for the router and controller Lambdas (Addendum §11.2).

Environment loading, a CloudWatch Embedded Metric Format (EMF) helper, and lazy boto3
client factories. Pool names and the default slots-per-unit come from ``src`` so there
is never a second definition of them under ``cloud/`` (NFR-A5).
"""

from __future__ import annotations

import functools
import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3

from src.config import LATENCY_DEFAULTS, PRIMITIVE_MAP
from src.controller.baseline_policy import BASELINE_POOL

# Five serving pools: one per primitive (aware policy) plus the homogeneous pool (baseline).
POOLS: tuple[str, ...] = (*PRIMITIVE_MAP.values(), BASELINE_POOL)


def _pool_env_suffix(pool: str) -> str:
    return pool.upper()


@dataclass(frozen=True)
class Settings:
    counts_table: str
    decisions_table: str
    results_table: str
    queue_urls: dict[str, str]  # pool -> SQS queue URL
    esm_uuids: dict[str, str]  # pool -> event-source-mapping UUID
    slots_per_unit: int
    time_scale: float
    enforce_governance: bool


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise KeyError(f"required environment variable {name} is not set")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read the Lambda's configuration from environment variables.

    Table names, queue URLs (``QUEUE_URL_<POOL>``) and ESM UUIDs (``ESM_UUID_<POOL>``)
    are required and raise ``KeyError`` when missing. ``SLOTS_PER_UNIT``, ``TIME_SCALE``
    and ``ENFORCE_GOVERNANCE`` default to the template's parameter defaults.
    """
    env = os.environ if env is None else env

    slots_per_unit = int(env.get("SLOTS_PER_UNIT", LATENCY_DEFAULTS["slots_per_unit"]))
    if slots_per_unit < 2:
        raise ValueError(f"SLOTS_PER_UNIT must be >= 2 (ESM MaximumConcurrency floor), got {slots_per_unit}")

    time_scale = float(env.get("TIME_SCALE", "1"))
    if time_scale <= 0:
        raise ValueError(f"TIME_SCALE must be positive, got {time_scale}")

    enforce_raw = env.get("ENFORCE_GOVERNANCE", "false").strip().lower()
    if enforce_raw not in ("true", "false"):
        raise ValueError(f"ENFORCE_GOVERNANCE must be 'true' or 'false', got {enforce_raw!r}")

    return Settings(
        counts_table=_required(env, "COUNTS_TABLE"),
        decisions_table=_required(env, "DECISIONS_TABLE"),
        results_table=_required(env, "RESULTS_TABLE"),
        queue_urls={p: _required(env, f"QUEUE_URL_{_pool_env_suffix(p)}") for p in POOLS},
        esm_uuids={p: _required(env, f"ESM_UUID_{_pool_env_suffix(p)}") for p in POOLS},
        slots_per_unit=slots_per_unit,
        time_scale=time_scale,
        enforce_governance=enforce_raw == "true",
    )


# EMF format per the CloudWatch spec:
# https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html
EMF_MAX_DIMENSIONS: int = 30
EMF_MAX_METRICS: int = 100


def emit_emf(
    namespace: str,
    dimensions: Mapping[str, str],
    metrics: Mapping[str, float],
    *,
    units: Mapping[str, str] | None = None,
    now: Callable[[], float] = time.time,
    write: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Print one EMF JSON line, extracted by CloudWatch Logs into custom metrics.

    Every distinct combination of dimension values becomes a separately billed custom
    metric, so dimensions must stay low-cardinality (never a request ID).
    Returns the document that was written, for tests.
    """
    if not dimensions:
        raise ValueError("emit_emf needs at least one dimension")
    if len(dimensions) > EMF_MAX_DIMENSIONS:
        raise ValueError(f"EMF allows at most {EMF_MAX_DIMENSIONS} dimensions, got {len(dimensions)}")
    if not metrics or len(metrics) > EMF_MAX_METRICS:
        raise ValueError(f"EMF needs 1..{EMF_MAX_METRICS} metrics, got {len(metrics)}")
    clashes = set(dimensions) & set(metrics)
    if clashes:
        raise ValueError(f"names used as both dimension and metric: {sorted(clashes)}")
    for key, value in dimensions.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"dimension {key!r} must be a non-empty string, got {value!r}")
    for key, metric_value in metrics.items():
        if not math.isfinite(float(metric_value)):
            raise ValueError(f"metric {key!r} must be finite, got {metric_value!r}")

    units = units or {}
    document: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(now() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [list(dimensions)],
                    "Metrics": [{"Name": name, "Unit": units.get(name, "None")} for name in metrics],
                }
            ],
        },
        **dict(dimensions),
        **{name: float(value) for name, value in metrics.items()},
    }
    write(json.dumps(document, separators=(",", ":")))
    return document


@functools.cache
def client(service: str) -> Any:
    """One boto3 client per service per container, created on first use, not at import."""
    return boto3.client(service)
