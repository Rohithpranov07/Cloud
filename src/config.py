"""Config loader — single source for the constants fixed by TRD §1.1, §1.3 and §1.6.

Anti-Hallucination rule 3: this module is the ONLY definition of ``ARCHETYPES``,
``PRIMITIVE_MAP``, ``PRIMITIVE_UNIT_CAPACITY`` and ``UNIT_COST_PER_MIN``. Every other
module imports them from here rather than restating them. The KEYS are fixed by
TRD §1.6; only the values in ``configs/default.yaml`` are tunable.

Added under Anti-Drift rule 6 as a shared root-level helper: it is not one of the
seven pipeline packages, so it does not violate the import-direction boundary in
``CLAUDE.md`` (every pipeline module may read config).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH: Path = REPO_ROOT / "configs" / "default.yaml"
DATA_DIR: Path = REPO_ROOT / "data"


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load and validate the YAML config.

    Raises ``FileNotFoundError`` if the file is missing and ``ValueError`` if the
    key invariants from TRD §1.1/§1.6 are violated. Errors are never swallowed
    (NFR-3).
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        cfg: dict[str, Any] = yaml.safe_load(handle)

    _validate(cfg, config_path)
    return cfg


def _validate(cfg: dict[str, Any], config_path: Path) -> None:
    """Fail loudly if the config drifts from the TRD's fixed keys."""
    archetypes: list[str] = cfg["archetypes"]
    if len(archetypes) != 4 or len(set(archetypes)) != 4:
        raise ValueError(f"{config_path}: TRD §1.1 fixes exactly four unique archetypes")

    if set(cfg["primitive_map"]) != set(archetypes):
        raise ValueError(f"{config_path}: primitive_map keys must be exactly the archetypes")

    primitives = set(cfg["primitive_map"].values())
    for table in ("primitive_unit_capacity", "unit_cost_per_min"):
        if set(cfg[table]) != primitives:
            raise ValueError(
                f"{config_path}: {table} keys must match PRIMITIVE_MAP's values exactly (TRD §1.6)"
            )

    for mix_name in ("mix_before", "mix_after"):
        mix: dict[str, float] = cfg["trace"][mix_name]
        if set(mix) != set(archetypes):
            raise ValueError(f"{config_path}: trace.{mix_name} must cover all four archetypes")
        if abs(sum(mix.values()) - 1.0) > 1e-9:
            raise ValueError(f"{config_path}: trace.{mix_name} proportions must sum to 1.0")


_CONFIG: dict[str, Any] = load_config()

# --- TRD §1.1 -------------------------------------------------------------------
ARCHETYPES: list[str] = list(_CONFIG["archetypes"])

# --- TRD §1.6 -------------------------------------------------------------------
PRIMITIVE_MAP: dict[str, str] = dict(_CONFIG["primitive_map"])
PRIMITIVE_UNIT_CAPACITY: dict[str, int] = dict(_CONFIG["primitive_unit_capacity"])
UNIT_COST_PER_MIN: dict[str, float] = dict(_CONFIG["unit_cost_per_min"])

# --- TRD §1.3 defaults ----------------------------------------------------------
TRACE_DEFAULTS: dict[str, Any] = dict(_CONFIG["trace"])
