# Implementation Guide: Semantic Workload-Composition-Aware Autoscaling — Core Prototype

Companion to `AWS-Project-Plan-PRD.docx`. This is the hands-on build guide: repo layout, environment setup, and a working code skeleton for every module in the Core Prototype, in the order you'll build them. Everything here runs locally in VS Code — no AWS account needed until the Stretch Goals section at the end.

---

## 0. Prerequisites

- **Python 3.11+** (check with `python3 --version`)
- **VS Code** with these extensions:
  - Python (Microsoft)
  - Pylance
  - Jupyter
  - GitLens (optional, helpful for two-person collaboration)
- **Git**, and a shared GitHub/GitLab repo for the two of you
- AWS CLI + an AWS account — **only needed for the Stretch Goal section**, not the Core Prototype

---

## 1. Project Setup

```bash
mkdir semantic-autoscaling && cd semantic-autoscaling
git init
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

Create `requirements.txt`:

```
numpy
pandas
scipy
matplotlib
scikit-learn
statsmodels
pytest
```

```bash
pip install -r requirements.txt
```

Open the folder in VS Code (`code .`), then set the interpreter: `Ctrl+Shift+P` → "Python: Select Interpreter" → pick the `.venv` one. This matters — without it, VS Code will use your system Python and imports will fail.

---

## 2. Repository Layout

```
semantic-autoscaling/
├── README.md
├── requirements.txt
├── configs/
│   └── default.yaml
├── src/
│   ├── trace_gen/
│   │   └── generator.py
│   ├── classifier/
│   │   └── archetype_classifier.py
│   ├── forecaster/
│   │   └── per_archetype_forecaster.py
│   ├── controller/
│   │   ├── baseline_policy.py
│   │   └── archetype_aware_policy.py
│   ├── cost_governance/
│   │   └── budget_projector.py
│   ├── feedback/
│   │   └── recalibration.py
│   └── evaluation/
│       └── run_comparison.py
├── data/                      # generated traces land here (gitignore this)
├── notebooks/
│   └── exploration.ipynb
└── tests/
    └── test_trace_gen.py
```

Add a `.gitignore`:

```
.venv/
__pycache__/
data/*.csv
.ipynb_checkpoints/
```

Commit this skeleton first, before writing any logic — gives you a clean starting point to branch from.

---

## 3. Module 1 — Trace Generator (`src/trace_gen/generator.py`)

This is the foundation everything else depends on — build and validate it first (Week 1–2 in the plan).

**What it needs to do:** produce a stream of timestamped requests, each tagged with a ground-truth archetype and a compute-per-request cost, with a controllable "compositional shift" event where the archetype mix changes while the aggregate rate stays flat.

```python
# src/trace_gen/generator.py
import numpy as np
import pandas as pd
from dataclasses import dataclass

ARCHETYPES = ["short_conversational", "long_context_rag", "agentic_tool_using", "batch_offline"]

# rough relative compute cost per request, by archetype (tune these)
COMPUTE_COST = {
    "short_conversational": 1.0,
    "long_context_rag": 3.5,
    "agentic_tool_using": 6.0,
    "batch_offline": 2.0,
}

@dataclass
class TraceConfig:
    duration_minutes: int = 120
    base_rate_per_min: float = 50.0          # aggregate requests/minute
    shift_start_min: int = 60                # when the compositional shift begins
    shift_duration_min: int = 15             # how long it takes to ramp
    mix_before: dict = None                  # archetype -> proportion
    mix_after: dict = None
    seed: int = 42

def _default_mix():
    return {"short_conversational": 0.70, "long_context_rag": 0.20,
            "agentic_tool_using": 0.05, "batch_offline": 0.05}

def _shifted_mix():
    return {"short_conversational": 0.40, "long_context_rag": 0.20,
            "agentic_tool_using": 0.35, "batch_offline": 0.05}

def generate_trace(cfg: TraceConfig = None) -> pd.DataFrame:
    cfg = cfg or TraceConfig()
    mix_before = cfg.mix_before or _default_mix()
    mix_after = cfg.mix_after or _shifted_mix()
    rng = np.random.default_rng(cfg.seed)

    rows = []
    req_id = 0
    for minute in range(cfg.duration_minutes):
        # aggregate rate stays constant through the shift — this is the point:
        # the shift is only visible in archetype composition, not request volume
        n_requests = rng.poisson(cfg.base_rate_per_min)

        if minute < cfg.shift_start_min:
            mix = mix_before
        elif minute < cfg.shift_start_min + cfg.shift_duration_min:
            t = (minute - cfg.shift_start_min) / cfg.shift_duration_min
            mix = {a: (1 - t) * mix_before[a] + t * mix_after[a] for a in ARCHETYPES}
        else:
            mix = mix_after

        probs = np.array([mix[a] for a in ARCHETYPES])
        probs = probs / probs.sum()
        archetypes = rng.choice(ARCHETYPES, size=n_requests, p=probs)

        for a in archetypes:
            # noisy per-request compute cost around the archetype's typical value
            cost = max(0.1, rng.normal(COMPUTE_COST[a], COMPUTE_COST[a] * 0.15))
            rows.append({
                "request_id": req_id,
                "minute": minute,
                "true_archetype": a,
                "compute_cost": cost,
                "tenant_id": rng.integers(0, 5),   # 5 simulated tenants
            })
            req_id += 1

    return pd.DataFrame(rows)

if __name__ == "__main__":
    df = generate_trace()
    df.to_csv("data/trace.csv", index=False)
    print(f"Generated {len(df)} requests over {df.minute.max()+1} minutes")
    print(df.groupby(["minute"]).size().describe())  # sanity check: aggregate rate is flat
```

**How to validate this module (do this before moving on):** plot aggregate requests/minute (should look flat/noisy-flat across the shift) next to archetype share/minute (should show a clear ramp at minute 60–75). If the aggregate plot shows a visible bump at the shift point, your compositional-shift design is broken — fix the rates before building anything downstream.

---

## 4. Module 2 — Archetype Classifier (`src/classifier/archetype_classifier.py`)

In the simulation, the classifier doesn't get `true_archetype` directly — it gets noisy proxy features (this mirrors your report's "pre-inference features" framing: prompt metadata, declared output length, tool-schema presence). Two options, pick based on time budget:

- **Rule-based (fastest to build, do this first):** derive proxy features that correlate with archetype and threshold on them.
- **Lightly-trained (do this if time allows, for a stronger "classifier" story):** a small `sklearn` decision tree trained on proxy features.

```python
# src/classifier/archetype_classifier.py
import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

def add_proxy_features(df: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    """Simulate the pre-inference features a real classifier would see,
    derived from true_archetype but with injected noise so the classifier
    isn't just reading the label directly."""
    rng = np.random.default_rng(seed)
    n = len(df)
    base_len = df["true_archetype"].map({
        "short_conversational": 50, "long_context_rag": 800,
        "agentic_tool_using": 300, "batch_offline": 1200,
    })
    df = df.copy()
    df["declared_output_len"] = np.maximum(1, base_len + rng.normal(0, 80, n))
    df["has_tool_schema"] = (
        (df["true_archetype"] == "agentic_tool_using").astype(int)
        ^ (rng.random(n) < 0.05).astype(int)   # 5% label noise
    )
    df["prompt_domain_score"] = rng.normal(
        df["true_archetype"].map({"short_conversational": 0.2, "long_context_rag": 0.8,
                                    "agentic_tool_using": 0.6, "batch_offline": 0.9}), 0.1)
    return df

def train_classifier(df: pd.DataFrame):
    features = df[["declared_output_len", "has_tool_schema", "prompt_domain_score"]]
    labels = df["true_archetype"]
    X_train, X_test, y_train, y_test = train_test_split(features, labels, test_size=0.2, random_state=0)
    clf = DecisionTreeClassifier(max_depth=6, random_state=0)
    clf.fit(X_train, y_train)
    acc = accuracy_score(y_test, clf.predict(X_test))
    print(f"Classifier held-out accuracy: {acc:.3f}")   # target: > 0.90 per the PRD acceptance check
    return clf

def classify(df: pd.DataFrame, clf) -> pd.DataFrame:
    df = df.copy()
    features = df[["declared_output_len", "has_tool_schema", "prompt_domain_score"]]
    df["predicted_archetype"] = clf.predict(features)
    df["confidence"] = clf.predict_proba(features).max(axis=1)
    return df

if __name__ == "__main__":
    trace = pd.read_csv("data/trace.csv")
    trace = add_proxy_features(trace)
    clf = train_classifier(trace)
    labeled = classify(trace, clf)
    labeled.to_csv("data/trace_labeled.csv", index=False)
```

---

## 5. Module 3 — Per-Archetype Forecaster (`src/forecaster/per_archetype_forecaster.py`)

Statistical forecasting per archetype (exponential smoothing), independently for each of the four archetypes, plus an "aggregate-only" version for the baseline comparison.

```python
# src/forecaster/per_archetype_forecaster.py
import pandas as pd
from statsmodels.tsa.holtwinters import SimpleExpSmoothing

def build_minute_series(df: pd.DataFrame, archetype_col: str) -> pd.DataFrame:
    """Requests per minute per archetype (or aggregate, if archetype_col=None)."""
    if archetype_col:
        pivot = df.groupby(["minute", archetype_col]).size().unstack(fill_value=0)
    else:
        pivot = df.groupby("minute").size().to_frame("aggregate")
    return pivot.reindex(range(df.minute.max() + 1), fill_value=0)

def forecast_series(series: pd.Series, horizon: int = 5) -> pd.Series:
    """One-step-ahead style forecast using simple exponential smoothing,
    rolled forward `horizon` minutes. Swap for a gradient-boosted model later if needed."""
    if series.sum() == 0:
        return pd.Series([0] * horizon)
    model = SimpleExpSmoothing(series.values, initialization_method="estimated").fit()
    return pd.Series(model.forecast(horizon))

def forecast_all_archetypes(df: pd.DataFrame, horizon: int = 5) -> dict:
    pivot = build_minute_series(df, "predicted_archetype")
    return {a: forecast_series(pivot[a], horizon) for a in pivot.columns}

def forecast_aggregate(df: pd.DataFrame, horizon: int = 5) -> pd.Series:
    pivot = build_minute_series(df, None)
    return forecast_series(pivot["aggregate"], horizon)

if __name__ == "__main__":
    labeled = pd.read_csv("data/trace_labeled.csv")
    per_archetype = forecast_all_archetypes(labeled)
    aggregate = forecast_aggregate(labeled)
    print("Per-archetype 5-min forecast:", {k: v.tolist() for k, v in per_archetype.items()})
    print("Aggregate-only 5-min forecast:", aggregate.tolist())
```

**This is where you'll actually see the paper's core claim demonstrated:** run this forecaster at minute ~58 (just before the shift). The `aggregate` forecast will look flat/unremarkable. The `agentic_tool_using` per-archetype forecast should already show a rising trend as the ramp begins. That divergence, captured in a plot, is your single most important evaluation result — put it early in your final report.

---

## 6. Module 4 — Capacity Controllers (baseline vs. archetype-aware)

Build the **baseline first** — it's simpler and gives you something to compare against immediately.

```python
# src/controller/baseline_policy.py
def baseline_scaling_decision(aggregate_forecast, current_capacity, per_unit_capacity=20):
    """STAR-style simplification: scale a single homogeneous pool based on
    aggregate forecast only, ignoring archetype composition entirely."""
    predicted_load = aggregate_forecast.iloc[0] if hasattr(aggregate_forecast, "iloc") else aggregate_forecast[0]
    required_units = max(1, round(predicted_load / per_unit_capacity))
    delta = required_units - current_capacity
    return {"action": "scale", "target_pool": "homogeneous_pool", "delta": delta,
            "new_capacity": required_units}
```

```python
# src/controller/archetype_aware_policy.py
PRIMITIVE_MAP = {
    "short_conversational": "bedrock_on_demand",
    "long_context_rag": "sagemaker_endpoint",
    "agentic_tool_using": "eks_gpu_reserved",   # long-lived sessions -> reserved pool
    "batch_offline": "eks_gpu_spot",
}
# rough relative capacity cost per request-per-minute, by primitive (tune to taste)
PRIMITIVE_UNIT_CAPACITY = {
    "bedrock_on_demand": 30, "sagemaker_endpoint": 15,
    "eks_gpu_reserved": 10, "eks_gpu_spot": 12,
}

def archetype_aware_scaling_decision(per_archetype_forecast, current_capacity: dict):
    """current_capacity: dict of primitive -> current units.
    Returns one scaling decision per primitive that needs to change."""
    decisions = []
    demand_by_primitive = {}
    for archetype, forecast in per_archetype_forecast.items():
        primitive = PRIMITIVE_MAP[archetype]
        predicted = forecast.iloc[0] if hasattr(forecast, "iloc") else forecast[0]
        demand_by_primitive[primitive] = demand_by_primitive.get(primitive, 0) + predicted

    for primitive, predicted_load in demand_by_primitive.items():
        required_units = max(1, round(predicted_load / PRIMITIVE_UNIT_CAPACITY[primitive]))
        current = current_capacity.get(primitive, 0)
        delta = required_units - current
        if delta != 0:
            decisions.append({"action": "scale", "target_pool": primitive,
                               "delta": delta, "new_capacity": required_units})
    return decisions
```

**Why the archetype-aware version should win in your evaluation:** during the shift window, `agentic_tool_using` demand rises while total aggregate stays flat. The baseline sees no reason to change the homogeneous pool. The archetype-aware controller sees rising demand specifically on `eks_gpu_reserved` and scales it *before* those (slower, more expensive) requests start queuing. That gap — in simulated latency or queue depth — is your headline metric.

---

## 7. Module 5 — Cost Governance (`src/cost_governance/budget_projector.py`)

```python
# src/cost_governance/budget_projector.py
UNIT_COST_PER_MIN = {
    "bedrock_on_demand": 0.02, "sagemaker_endpoint": 0.05,
    "eks_gpu_reserved": 0.09, "eks_gpu_spot": 0.04,
}

def project_spend(capacity_by_primitive: dict, minutes_remaining: int) -> float:
    return sum(units * UNIT_COST_PER_MIN[p] * minutes_remaining
               for p, units in capacity_by_primitive.items())

def check_budget(projected_spend: float, budget: float) -> dict:
    if projected_spend > budget:
        overage = projected_spend - budget
        action = "throttle" if overage / budget > 0.2 else "downgrade_tier"
        return {"breach_projected": True, "action": action, "overage": overage}
    return {"breach_projected": False, "action": "none", "overage": 0.0}
```

Run this against both controllers' capacity decisions on the same trace, with the same `budget`. Log the **minute at which each policy's projection first flags a breach**, and compare it to the minute the *baseline's realized* (not projected) cost would actually cross the budget. The gap between those two minutes is your "lead time" metric from the PRD.

---

## 8. Module 6 — Feedback Loop (`src/feedback/recalibration.py`)

Keep this simple for the Core Prototype — a periodic re-fit is enough to demonstrate the mechanism:

```python
# src/feedback/recalibration.py
from src.classifier.archetype_classifier import train_classifier

def recalibrate(all_labeled_so_far_df):
    """Re-fit the classifier on everything observed so far, including
    corrected labels from post-inference telemetry (here, just re-using
    true_archetype as a stand-in for 'ground truth observed after the fact')."""
    return train_classifier(all_labeled_so_far_df)
```

In your evaluation script, call this every N simulated minutes and log accuracy before/after — that trend line (accuracy improving over the run) is the whole deliverable for this module.

---

## 9. Module 7 — Evaluation (`src/evaluation/run_comparison.py`)

This ties everything together and produces the plots/tables for your final report.

```python
# src/evaluation/run_comparison.py
import pandas as pd
import matplotlib.pyplot as plt
from src.trace_gen.generator import generate_trace, TraceConfig
from src.classifier.archetype_classifier import add_proxy_features, train_classifier, classify
from src.forecaster.per_archetype_forecaster import forecast_all_archetypes, forecast_aggregate
from src.controller.baseline_policy import baseline_scaling_decision
from src.controller.archetype_aware_policy import archetype_aware_scaling_decision
from src.cost_governance.budget_projector import project_spend, check_budget

def run():
    trace = generate_trace(TraceConfig())
    trace = add_proxy_features(trace)
    clf = train_classifier(trace)
    labeled = classify(trace, clf)

    results = []
    baseline_capacity = 3
    aware_capacity = {"bedrock_on_demand": 2, "sagemaker_endpoint": 1,
                        "eks_gpu_reserved": 1, "eks_gpu_spot": 1}

    for minute in range(10, labeled.minute.max()):
        window = labeled[labeled.minute <= minute]

        agg_fc = forecast_aggregate(window)
        base_decision = baseline_scaling_decision(agg_fc, baseline_capacity)
        baseline_capacity = base_decision["new_capacity"]

        per_arch_fc = forecast_all_archetypes(window)
        aware_decisions = archetype_aware_scaling_decision(per_arch_fc, aware_capacity)
        for d in aware_decisions:
            aware_capacity[d["target_pool"]] = d["new_capacity"]

        baseline_spend = project_spend({"bedrock_on_demand": baseline_capacity}, minutes_remaining=10)
        aware_spend = project_spend(aware_capacity, minutes_remaining=10)

        results.append({
            "minute": minute,
            "baseline_capacity": baseline_capacity,
            "aware_capacity_total": sum(aware_capacity.values()),
            "baseline_projected_spend": baseline_spend,
            "aware_projected_spend": aware_spend,
        })

    results_df = pd.DataFrame(results)
    results_df.to_csv("data/evaluation_results.csv", index=False)

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(results_df.minute, results_df.baseline_capacity, label="Baseline (aggregate-only)")
    ax.plot(results_df.minute, results_df.aware_capacity_total, label="Archetype-aware")
    ax.axvline(60, color="gray", linestyle="--", label="Compositional shift begins")
    ax.set_xlabel("Minute"); ax.set_ylabel("Provisioned capacity units"); ax.legend()
    ax.set_title("Capacity Response: Baseline vs. Archetype-Aware Policy")
    plt.savefig("data/capacity_comparison.png", dpi=150, bbox_inches="tight")
    print("Saved data/evaluation_results.csv and data/capacity_comparison.png")

if __name__ == "__main__":
    run()
```

Run it:

```bash
python -m src.evaluation.run_comparison
```

This is the script your final report's "Implementation and Results" section is built around — the CSV gives you your tables, the PNG gives you your headline figure.

---

## 10. Working in VS Code Day-to-Day

- **Debugging:** create `.vscode/launch.json`:
  ```json
  {
    "version": "0.2.0",
    "configurations": [
      {
        "name": "Run evaluation",
        "type": "debugpy",
        "request": "launch",
        "module": "src.evaluation.run_comparison",
        "console": "integratedTerminal"
      }
    ]
  }
  ```
  Set breakpoints directly in `run_comparison.py` and step through with F5.
- **Exploration:** use `notebooks/exploration.ipynb` (Jupyter extension) for one-off plots and sanity checks — keep the actual pipeline in `.py` files under `src/`, not in the notebook, so it stays testable and reviewable.
- **Tests:** put at least a couple of sanity checks in `tests/`, e.g.:
  ```python
  # tests/test_trace_gen.py
  from src.trace_gen.generator import generate_trace, TraceConfig

  def test_aggregate_rate_stays_flat_through_shift():
      df = generate_trace(TraceConfig(duration_minutes=90, seed=1))
      per_minute = df.groupby("minute").size()
      assert per_minute.std() / per_minute.mean() < 0.3  # roughly flat, allowing for Poisson noise
  ```
  Run with `pytest` from the integrated terminal.
- **Git workflow for two people:** one branch per module (`feature/trace-gen`, `feature/classifier`, …), merge into `main` as each module passes its own sanity check, small frequent commits rather than one giant commit at the end.

---

## 11. Stretch Goals (only after the Core Prototype above is fully working)

**A. Real STAR-style RL policy.** Replace `archetype_aware_policy.py`'s heuristic with an actual GAT + Transformer policy: `torch_geometric` for the GAT layers, a small Transformer encoder (or just `nn.TransformerEncoder` from PyTorch) over each pool's request-count history, trained with an evolutionary strategy library (e.g., `cma` or a hand-rolled ES loop per the original STAR paper). This is a multi-week addition by itself — budget time accordingly and only attempt it once the Core Prototype's evaluation is already done and safe.

**B. Live AWS deployment.**
1. Set a AWS Budgets alert **first**, before deploying anything (Console → Billing → Budgets → create a budget with an email alert at e.g. $10).
2. Deploy the classifier as a Lambda function (`sam init` or plain `boto3`/CDK), fronted by API Gateway.
3. Point it at a small SageMaker Serverless Inference endpoint (not a persistent GPU instance — stays within free-tier-adjacent cost).
4. Wire CloudWatch custom metrics for the feedback loop.
5. Tear everything down (`terraform destroy` / manual console cleanup) immediately after taking the screenshots/metrics you need for the report — don't leave endpoints running.

---

## 12. Suggested Weekly Checklist (maps to the PRD milestones)

| Week | Deliverable in repo | Command to verify |
|---|---|---|
| 1–2 | `src/trace_gen/generator.py` + `tests/test_trace_gen.py` passing | `pytest tests/test_trace_gen.py` |
| 3 | `src/classifier/archetype_classifier.py`, accuracy printed >0.90 | `python -m src.classifier.archetype_classifier` |
| 4–5 | `src/forecaster/`, `src/controller/baseline_policy.py` | `python -m src.forecaster.per_archetype_forecaster` |
| 6–7 | `src/controller/archetype_aware_policy.py`, `src/cost_governance/` | `python -m src.evaluation.run_comparison` (partial) |
| 8 | `src/feedback/recalibration.py`, full `run_comparison.py`, plots generated | `python -m src.evaluation.run_comparison` |
| 9–10 | Report write-up from `data/evaluation_results.csv` + `data/capacity_comparison.png`; stretch goals if time remains | — |
