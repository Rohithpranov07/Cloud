# TRD — Semantic Workload-Composition-Aware Predictive Autoscaling & Cost-Governance (Core Prototype)
### Technical Requirements Document — single source of truth for the Build Instructions

> This TRD plays the same role for this project that the "Technical Deep-Dive" plays for HELIX: it is the canonical spec every Claude Code task in `Build-Instructions.md` imports from. If a prompt ever seems to disagree with this document, **this document wins** — stop and reconcile before continuing.
>
> Scope: the **Core Prototype** only (local Python simulation, no AWS account required), as scoped in `AWS-Project-Plan-PRD.docx`. AWS deployment is Phase 9 (Stretch) and is explicitly out of scope for "done" on this TRD.

---

## 0. System Context

- **Course artifact this implements:** `BCSE355L_Project_Report.pdf`, Section 4 (System Architecture).
- **Base paper:** Fang, Z., Ma, H., Chen, G., & Chen, S. (2026). *STAR: Spatial-Temporal Autoscaling for Cloud Applications with Deep Reinforcement Learning.* Expert Systems With Applications, 319, 132105.
- **Companion patent disclosure:** `AWS-Cloud-Architecture-Patent-Disclosure.docx` — the invention this prototype provides reduction-to-practice evidence for.
- **Core claim under test:** conditioning autoscaling and cost-governance decisions on the *semantic archetype* of incoming requests (not just aggregate request rate) reduces simulated latency-SLA violations and/or improves cost-budget adherence relative to an archetype-agnostic baseline modeled on STAR's original formulation.
- **What "done" means for this TRD:** `python -m src.evaluation.run_comparison` runs end-to-end on a generated trace and produces `data/evaluation_results.csv` + `data/capacity_comparison.png` showing the archetype-aware policy detecting and responding to a compositional shift that the baseline misses.

---

## 1. Canonical Data Models

> These are the field names and types every module MUST use. Do not rename fields, do not invent alternate shapes. Prefer `@dataclass` for internal objects; `pandas.DataFrame` columns must use exactly these names.

### 1.1 Archetypes (fixed enum)

```python
ARCHETYPES = ["short_conversational", "long_context_rag", "agentic_tool_using", "batch_offline"]
```

Do not add, remove, or rename archetypes without updating this TRD first — every downstream module (classifier, forecaster, controller, cost table) indexes on these exact strings.

### 1.2 `Request` (one row of the trace DataFrame)

| Field | Type | Notes |
|---|---|---|
| `request_id` | `int` | monotonically increasing |
| `minute` | `int` | simulation minute, 0-indexed |
| `true_archetype` | `str` | one of `ARCHETYPES` — ground truth, classifier must NOT read this directly |
| `compute_cost` | `float` | simulated per-request compute units |
| `tenant_id` | `int` | 0–4 in the Core Prototype (5 simulated tenants) |
| `declared_output_len` | `float` | proxy feature, added by classifier stage |
| `has_tool_schema` | `int` (0/1) | proxy feature |
| `prompt_domain_score` | `float` | proxy feature |
| `predicted_archetype` | `str` | classifier output, one of `ARCHETYPES` |
| `confidence` | `float` | classifier's max class probability, `[0,1]` |

### 1.3 `TraceConfig`

```python
@dataclass
class TraceConfig:
    duration_minutes: int = 120
    base_rate_per_min: float = 50.0
    shift_start_min: int = 60
    shift_duration_min: int = 15
    mix_before: dict | None = None   # archetype -> proportion, sums to 1.0
    mix_after: dict | None = None
    seed: int = 42
```

**Invariant (must hold and be tested):** the *aggregate* requests/minute series must remain approximately flat (`std/mean < 0.3`, Poisson-noise tolerance) across the shift window. The shift is visible ONLY in per-archetype composition. This invariant is the entire experimental premise — if it doesn't hold, nothing built on top of it is valid.

### 1.4 `ForecastVector`

Per-archetype forecaster returns `dict[str, pandas.Series]`, one key per archetype in `ARCHETYPES`, each a `Series` of length `horizon` (forecasted requests/minute). Aggregate-only forecaster returns a single `pandas.Series` of length `horizon`.

### 1.5 `ScalingDecision`

```python
{
  "action": "scale",              # fixed literal for Core Prototype
  "target_pool": str,             # baseline: "homogeneous_pool"; aware: one of PRIMITIVE_MAP.values()
  "delta": int,                   # signed change in capacity units
  "new_capacity": int,            # >= 1
}
```

### 1.6 `PRIMITIVE_MAP` and unit tables (fixed for Core Prototype)

```python
PRIMITIVE_MAP = {
    "short_conversational": "bedrock_on_demand",
    "long_context_rag": "sagemaker_endpoint",
    "agentic_tool_using": "eks_gpu_reserved",
    "batch_offline": "eks_gpu_spot",
}
PRIMITIVE_UNIT_CAPACITY = {
    "bedrock_on_demand": 30, "sagemaker_endpoint": 15,
    "eks_gpu_reserved": 10, "eks_gpu_spot": 12,
}
UNIT_COST_PER_MIN = {
    "bedrock_on_demand": 0.02, "sagemaker_endpoint": 0.05,
    "eks_gpu_reserved": 0.09, "eks_gpu_spot": 0.04,
}
```

Tunable via `configs/default.yaml` (Task T1.0), but the KEYS are fixed — every primitive name must match `PRIMITIVE_MAP`'s values exactly.

### 1.7 `BudgetCheckResult`

```python
{
  "breach_projected": bool,
  "action": Literal["none", "downgrade_tier", "throttle"],
  "overage": float,   # >= 0.0
}
```

### 1.8 `EvaluationRow` (one row of `data/evaluation_results.csv`)

| Field | Type |
|---|---|
| `minute` | `int` |
| `baseline_capacity` | `int` |
| `aware_capacity_total` | `int` |
| `baseline_projected_spend` | `float` |
| `aware_projected_spend` | `float` |

Additional columns may be ADDED (e.g., per-primitive breakdown, lead-time flags) but these five must always be present under these exact names, since the evaluation write-up references them directly.

---

## 2. Module Contracts (function signatures — do not deviate)

```python
# src/trace_gen/generator.py
def generate_trace(cfg: TraceConfig = None) -> pd.DataFrame: ...

# src/classifier/archetype_classifier.py
def add_proxy_features(df: pd.DataFrame, seed: int = 0) -> pd.DataFrame: ...
def train_classifier(df: pd.DataFrame) -> DecisionTreeClassifier: ...
def classify(df: pd.DataFrame, clf) -> pd.DataFrame: ...

# src/forecaster/per_archetype_forecaster.py
def build_minute_series(df: pd.DataFrame, archetype_col: str | None) -> pd.DataFrame: ...
def forecast_series(series: pd.Series, horizon: int = 5) -> pd.Series: ...
def forecast_all_archetypes(df: pd.DataFrame, horizon: int = 5) -> dict[str, pd.Series]: ...
def forecast_aggregate(df: pd.DataFrame, horizon: int = 5) -> pd.Series: ...

# src/controller/baseline_policy.py
def baseline_scaling_decision(aggregate_forecast, current_capacity: int, per_unit_capacity: int = 20) -> dict: ...

# src/controller/archetype_aware_policy.py
def archetype_aware_scaling_decision(per_archetype_forecast: dict, current_capacity: dict) -> list[dict]: ...

# src/cost_governance/budget_projector.py
def project_spend(capacity_by_primitive: dict, minutes_remaining: int) -> float: ...
def check_budget(projected_spend: float, budget: float) -> dict: ...

# src/feedback/recalibration.py
def recalibrate(all_labeled_so_far_df: pd.DataFrame): ...

# src/evaluation/run_comparison.py
def run() -> None: ...   # writes data/evaluation_results.csv and data/capacity_comparison.png
```

Any task that needs a function NOT listed here must propose the addition explicitly (name, signature, file) before implementing it — see Anti-Drift Rule 6 in `Build-Instructions.md` §A.

---

## 3. Repo Structure (fixed)

```
semantic-autoscaling/
├── README.md
├── CLAUDE.md
├── requirements.txt
├── configs/
│   └── default.yaml
├── src/
│   ├── trace_gen/generator.py
│   ├── classifier/archetype_classifier.py
│   ├── forecaster/per_archetype_forecaster.py
│   ├── controller/baseline_policy.py
│   ├── controller/archetype_aware_policy.py
│   ├── cost_governance/budget_projector.py
│   ├── feedback/recalibration.py
│   └── evaluation/run_comparison.py
├── data/                      # generated at runtime, gitignored except .gitkeep
├── notebooks/exploration.ipynb
└── tests/
    ├── test_trace_gen.py
    ├── test_classifier.py
    ├── test_forecaster.py
    ├── test_controller.py
    ├── test_cost_governance.py
    └── test_evaluation_end_to_end.py
```

---

## 4. Pinned Stack

```
python >= 3.11
numpy
pandas
scipy
matplotlib
scikit-learn
statsmodels
pytest
pyyaml
```

**Instruction to Claude Code:** confirm the latest mutually compatible versions at install time, pin exact resolved versions in `requirements.txt` (`==`, not `>=`), and do not bump versions across tasks without saying so explicitly.

---

## 5. Non-Functional Requirements (carried from the PRD, restated as testable)

- **NFR-1 Reproducibility:** `python -m src.evaluation.run_comparison` must run end-to-end from a clean clone + `pip install -r requirements.txt`, with no manual steps and no network access required.
- **NFR-2 Determinism:** every stochastic component (`generate_trace`, `add_proxy_features`, classifier train/test split) must accept and respect a `seed`, so two runs with the same seed produce identical output.
- **NFR-3 No silent failures:** no bare `except:` blocks anywhere; forecasting/classification errors must raise or be explicitly handled and logged, never swallowed.
- **NFR-4 Type hints:** every function in `src/` has full type hints on parameters and return value; run `mypy src/` clean (or documented exceptions with a comment explaining why).
- **NFR-5 Cost ceiling (Stretch phase only):** any AWS resource created in Phase 9 must be behind a pre-created AWS Budgets alert, and torn down within the same working session.

---

## 6. Acceptance Criteria per Module (mirrors PRD §4, restated as pass/fail gates)

| Module | Acceptance Gate |
|---|---|
| Trace Generator | Aggregate requests/minute `std/mean < 0.3` across the full duration; archetype-share plot shows a visible ramp at `shift_start_min` for at least one archetype. |
| Classifier | `accuracy_score` on held-out split `> 0.90`. |
| Forecaster | Per-archetype forecast for the shifting archetype shows a rising trend starting within 5 minutes of `shift_start_min`; aggregate-only forecast does NOT show an equivalent trend at the same point. |
| Baseline Controller | Produces a valid `ScalingDecision` every call; capacity never goes below 1. |
| Archetype-Aware Controller | Produces at least one `ScalingDecision` targeting `eks_gpu_reserved` with positive `delta` within 10 minutes of `shift_start_min`. |
| Cost Governance | `check_budget` correctly flags `breach_projected: True` when `projected_spend > budget`, `False` otherwise (property-based or table-driven test). |
| Feedback Loop | Classifier accuracy after `recalibrate()` on an expanded window is `>=` accuracy before, on at least 2 of 3 re-fits in a full run. |
| Evaluation | `run()` completes without error and produces both output files; `evaluation_results.csv` has all five `EvaluationRow` columns; the capacity-comparison plot visually diverges between baseline and aware series after `shift_start_min`. |

---

## 7. Explicit Non-Goals for This TRD

- Training the full STAR GAT + Transformer + ESRL policy (Stretch Goal, separate TRD section if attempted).
- Any live AWS deployment (Phase 9 / Stretch).
- Multi-region, multi-account, or real multi-tenant billing integration.
- Anything not already listed in `AWS-Project-Plan-PRD.docx` §2.1.
