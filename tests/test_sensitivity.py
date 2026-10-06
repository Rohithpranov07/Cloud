"""Smoke test for the sensitivity grid — Addendum §10.2. One cell, one seed only; the
full grid (240 runs) is exercised via ``python -m src.evaluation.sensitivity``, not in
the test suite.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.evaluation.sensitivity import (
    _assert_mix_is_valid,
    _mix_after_for_agentic_share,
    _run_cell_seed,
)
from src.trace_gen.generator import _default_config


def test_mix_after_for_agentic_share_sums_to_one_and_moves_only_the_two_archetypes() -> None:
    default_mix = _default_config().mix_after or {}
    for agentic_share in (0.20, 0.30, 0.40, 0.50):
        mix = _mix_after_for_agentic_share(agentic_share)
        assert abs(sum(mix.values()) - 1.0) < 1e-9
        assert mix["agentic_tool_using"] == agentic_share
        assert mix["long_context_rag"] == default_mix["long_context_rag"]
        assert mix["batch_offline"] == default_mix["batch_offline"]


def test_assert_mix_is_valid_accepts_every_grid_point() -> None:
    for agentic_share in (0.20, 0.30, 0.40, 0.50):
        mix = _mix_after_for_agentic_share(agentic_share)
        cfg = replace(_default_config(), mix_after=mix)
        _assert_mix_is_valid(cfg)  # must not raise


def test_run_cell_seed_one_cell_one_seed() -> None:
    """One (seed, sizing, agentic_share, sla_multiplier) cell runs end to end and
    produces a sensible row.
    """
    row = _run_cell_seed(seed=0, sizing="ceil", agentic_share=0.50, sla_multiplier=3.0)

    assert row["seed"] == 0
    assert row["sizing"] == "ceil"
    assert row["agentic_share"] == 0.50
    assert row["sla_multiplier"] == 3.0
    for key in ("baseline_sla_violation_rate", "aware_sla_violation_rate"):
        assert 0.0 <= row[key] <= 1.0
    assert row["difference_baseline_minus_aware"] == pytest.approx(
        row["baseline_sla_violation_rate"] - row["aware_sla_violation_rate"]
    )
