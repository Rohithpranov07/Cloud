"""Golden-file regression test — Addendum §9.3 / §13 "Sizing refactor" gate.

Proves ``sizing="round"`` (the default on both controllers) still reproduces Part 1
output byte-for-byte, so the T10.1 keyword addition cannot silently change the
headline numbers the README and the report already cite.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.config import DATA_DIR
from src.evaluation.run_comparison import RESULTS_PATH, run

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "evaluation_results_round.csv"


def test_default_sizing_reproduces_the_golden_evaluation_csv() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    run()
    produced = pd.read_csv(RESULTS_PATH)
    golden = pd.read_csv(GOLDEN_PATH)
    pd.testing.assert_frame_equal(produced, golden)
