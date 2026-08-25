"""Cost-governance layer — TRD §1.6 (cost table), §1.7 (BudgetCheckResult) and §2.

Projects forward spend from the capacity the controller just decided on, and decides
whether a policy action is needed BEFORE the budget is actually breached. The lead time
this buys is the secondary success metric in PRD §8: an archetype-aware projection sees a
composition shift toward expensive primitives (eks_gpu_reserved at 0.09/unit/min) while an
aggregate-only projection, looking at a flat request rate, sees nothing to worry about.

The cost table is read from ``configs/default.yaml`` via ``src.config`` (TRD §1.6).
"""

from __future__ import annotations

from typing import Literal

from src.config import UNIT_COST_PER_MIN

# Overage beyond this fraction of the budget escalates from a soft action to a hard one.
THROTTLE_THRESHOLD: float = 0.2


def project_spend(capacity_by_primitive: dict, minutes_remaining: int) -> float:
    """Project spend over the remaining window for a given capacity allocation.

    Sums ``units * UNIT_COST_PER_MIN[primitive] * minutes_remaining`` across the dict.
    Unknown primitive names raise rather than being skipped, so a typo can never silently
    under-report cost (NFR-3).
    """
    if minutes_remaining < 0:
        raise ValueError(f"minutes_remaining cannot be negative, got {minutes_remaining}")

    unknown = set(capacity_by_primitive) - set(UNIT_COST_PER_MIN)
    if unknown:
        raise KeyError(f"unknown primitives in capacity dict: {sorted(unknown)}")

    total = 0.0
    for primitive, units in capacity_by_primitive.items():
        if units < 0:
            raise ValueError(f"capacity for '{primitive}' cannot be negative, got {units}")
        total += float(units) * UNIT_COST_PER_MIN[primitive] * minutes_remaining
    return total


def check_budget(projected_spend: float, budget: float) -> dict:
    """Decide whether projected spend breaches the budget, and what to do about it.

    Returns a BudgetCheckResult exactly as shaped in TRD §1.7:
    ``{"breach_projected": bool, "action": "none"|"downgrade_tier"|"throttle", "overage": float}``.

    A small breach downgrades tenants to a cheaper serving tier; a breach beyond
    ``THROTTLE_THRESHOLD`` of the budget escalates to throttling.
    """
    if budget <= 0:
        raise ValueError(f"budget must be positive, got {budget}")
    if projected_spend < 0:
        raise ValueError(f"projected_spend cannot be negative, got {projected_spend}")

    breach_projected = projected_spend > budget
    overage = max(0.0, projected_spend - budget)

    action: Literal["none", "downgrade_tier", "throttle"]
    if not breach_projected:
        action = "none"
    elif overage / budget > THROTTLE_THRESHOLD:
        action = "throttle"
    else:
        action = "downgrade_tier"

    return {"breach_projected": breach_projected, "action": action, "overage": overage}
