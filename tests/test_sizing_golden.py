"""Golden-file regression test — Addendum §9.3 / §13 "Sizing refactor" gate.

Proves ``sizing="round"`` (the default on both controllers) still reproduces Part 1
output byte-for-byte, so the T10.1 keyword addition cannot silently change the
headline numbers the README and the report already cite.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.evaluation.run_comparison import simulate
from src.trace_gen.generator import _default_config

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "evaluation_results_round.csv"


def test_default_sizing_reproduces_the_golden_evaluation_csv() -> None:
    """``sizing="round"`` must still reproduce Part 1 byte-for-byte (Addendum §13).

    T10.3 added ``baseline_actual_spend``, a genuinely new column (Addendum §9.5) that
    the frozen golden file predates — so this compares only the golden file's own
    columns, which is exactly the set the sizing refactor must not change.
    """
    results, _ = simulate(_default_config(), sizing="round")
    golden = pd.read_csv(GOLDEN_PATH)
    pd.testing.assert_frame_equal(results[list(golden.columns)], golden)
