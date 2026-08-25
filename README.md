# Semantic Workload-Composition-Aware Predictive Autoscaling — Core Prototype

A local Python simulation testing one claim: conditioning cloud autoscaling and
cost-governance decisions on the **semantic archetype** of incoming AI inference requests
— not just the aggregate request rate — reduces simulated latency-SLA violations and
improves cost-budget adherence, versus an archetype-agnostic baseline modelled on the
STAR base paper's formulation.

The experiment is built around a workload that shifts **composition without shifting
volume**: the request rate stays flat at ~50 req/min while the mix moves from 10% to 40%
agentic traffic. An aggregate-only autoscaler cannot see that by construction. The whole
prototype exists to measure what that blindness costs.

**Course:** BCSE355L — Cloud Architecture Design
**Team:** V Rohith Pranov (24BCE0619), Sudir Niranj R (24BDS0267)
**Base paper:** Fang, Z., Ma, H., Chen, G., & Chen, S. (2026). *STAR: Spatial-Temporal
Autoscaling for Cloud Applications with Deep Reinforcement Learning.* Expert Systems With
Applications, 319, 132105.

---

## Setup

Requires Python 3.11 or newer (verified on 3.14.7, macOS/arm64).

```bash
git clone https://github.com/Rohithpranov07/Cloud.git
cd Cloud
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

In VS Code: `Ctrl+Shift+P` → "Python: Select Interpreter" → pick the `.venv` one.
Without this, VS Code uses the system Python and the `src.` imports fail.

## Run the whole thing

One command reproduces every number in this README:

```bash
python -m src.evaluation.run_comparison
```

It generates the trace, trains the classifier, runs both policies head-to-head minute by
minute, recalibrates on a rolling basis, and writes:

| Output | Contents |
|---|---|
| `data/evaluation_results.csv` | One row per simulated minute — both policies' capacity, projected spend, breach flags, and per-primitive breakdown. |
| `data/capacity_comparison.png` | Baseline vs. archetype-aware capacity and projected spend, with the shift window and budget marked. |

## Run each phase individually

Each module is runnable on its own and writes its own artifact:

```bash
python -m src.trace_gen.generator            # -> data/trace.csv          (Phase 1)
python -m src.classifier.archetype_classifier # -> data/trace_labeled.csv (Phase 2)
python -m src.forecaster.per_archetype_forecaster  # prints both forecasts (Phase 3)
python -m src.evaluation.run_comparison      # -> the two outputs above   (Phase 7)
```

`notebooks/exploration.ipynb` validates the core experimental invariant visually and
writes `data/trace_aggregate_rate.png` and `data/trace_archetype_shares.png`.

## Tests

```bash
pytest -v          # 212 tests
mypy src/          # clean
```

The suite includes the acceptance gate for every module (TRD §6) plus
`tests/test_nfr_compliance.py`, which enforces the non-functional requirements — import
direction, seeding, no bare excepts, full type hints, exact dependency pins — as tests
rather than as a one-time audit.

---

## Results

From the last verified run of `python -m src.evaluation.run_comparison` (seed 42, default
config). Every figure below is reproducible from a clean clone by the command above.

### Primary result — capacity response

The shift begins at minute 60. Aggregate request rate never changes.

| | pre-shift | post-shift | change |
|---|---:|---:|---:|
| baseline capacity (archetype-agnostic) | 2.20 | 2.05 | **−6.8%** |
| archetype-aware capacity | 4.00 | 4.77 | **+19.2%** |

Normalised capacity divergence after the shift: **0.2598**.

The aware policy also scales the *right* primitive rather than just scaling up:

| Archetype | Primitive | pre-shift | post-shift |
|---|---|---:|---:|
| `agentic_tool_using` | `eks_gpu_reserved` | 1.00 | **1.77** |
| `short_conversational` | `bedrock_on_demand` | 1.00 | 1.00 |
| `long_context_rag` | `sagemaker_endpoint` | 1.00 | 1.00 |
| `batch_offline` | `eks_gpu_spot` | 1.00 | 1.00 |

The baseline does not move, because at constant aggregate request rate there is nothing
in its input to move. That is the finding, not a bug.

### Secondary result — budget-breach lead time

| Policy | First projects a breach |
|---|---|
| archetype-aware | **minute 71** (action: `downgrade_tier`) |
| aggregate-only | minute 108 |

**Lead time: 37 minutes.** By minute 108 more than 20 of the 24-unit budget is already
spent, so the aggregate-only view does not so much *detect* the overrun as *discover* it
once it has largely happened.

### Supporting measurements

| Module | Gate (TRD §6) | Measured |
|---|---|---|
| Trace generator | aggregate `std/mean < 0.3` | **0.1363** — agentic share 0.102 → 0.391, aggregate mean 49.92 → 49.36 |
| Classifier | held-out accuracy `> 0.90` | **0.9675** (whole-trace 0.9734) |
| Forecaster | per-archetype detects, aggregate does not | agentic forecast **+312%** vs baseline by minute 80; aggregate stays within **±1%** and never detects |
| Feedback loop | accuracy holds or improves on ≥2 of 3 re-fits | **5 of 5** checkpoints improved (e.g. 0.9779 → 0.9857) |

### Two deviations, recorded rather than hidden

1. **Forecaster detection at +7 min, not +5.** TRD §6 words the gate as a rising trend
   "within 5 minutes of `shift_start_min`". Measured: 7 minutes. Forcing the smoothing
   level higher does buy those two minutes, but it lifts the pre-shift noise floor from
   ~3% to ~42–59% — larger than the effect being detected. A 25:1 signal-to-noise ratio
   at +7 minutes beats a 2:1 ratio at +5, so the un-tuned configuration is kept.
   Rationale is in the `per_archetype_forecaster.py` docstring.

2. **Aware controller acts at +12 min, not +10.** The forecaster detects at +7, but
   `eks_gpu_reserved` serves 10 req/min and the TRD-mandated `round` sizing rule withholds
   a second unit until demand exceeds 15. Under `ceil` the same scale-up lands at +8,
   inside the gate — but `ceil` for the aware policy while the baseline uses TRD-mandated
   `round` would mean part of the measured improvement came from the rounding rule rather
   than from archetype awareness. The two policies must differ **only** in whether they
   can see composition, so `round` is used in both. Resolving this properly needs a human
   decision (apply `ceil` to both, deviating from TRD §2, or relax the §6 gate); the TRD
   is followed as written until then. Rationale is at the top of
   `archetype_aware_policy.py`.

### How the comparison is kept honest

The aware policy receives a signal the baseline does not. That is the point of the
experiment, not a flaw in it (PRD §9). Everything else is held identical: the same trace,
the same forecast horizon, the same sizing and rounding rules, the same cost table. The
baseline's homogeneous pool is priced at a blended rate **derived** from the config —
per-archetype cost per served request/minute, weighted by the pre-shift mix
(0.04867/unit/min) — and a test asserts that rate sits strictly between the cheapest and
dearest primitive so it cannot be a hidden advantage either way. The budget (24.0) is set
the way a tenant would set one: the pre-shift steady-state run rate (22.20) plus ~8%
headroom, so a workload that never changes composition stays inside it.

---

## How this maps to the report

| Module | Report §4 subsection | Role |
|---|---|---|
| `src/trace_gen/generator.py` | Ingress / workload model | Synthetic multi-tenant request stream with a compositional shift at constant volume |
| `src/classifier/archetype_classifier.py` | Semantic Archetype Classifier | Labels each request pre-inference from proxy features only |
| `src/forecaster/per_archetype_forecaster.py` | Predictive demand layer | Independent per-archetype forecast vs. the aggregate-only baseline signal |
| `src/controller/baseline_policy.py` | STAR baseline formulation | Archetype-agnostic control over one homogeneous pool |
| `src/controller/archetype_aware_policy.py` | Capacity controller | Routes demand to AWS serving primitives via `PRIMITIVE_MAP` and sizes each independently |
| `src/cost_governance/budget_projector.py` | Cost-governance loop | Projects end-of-window spend and escalates none → downgrade → throttle |
| `src/feedback/recalibration.py` | Feedback / recalibration | Re-fits the classifier on an expanding telemetry window |
| `src/evaluation/run_comparison.py` | Evaluation | Head-to-head run producing the two results above |

## Repository layout

```
├── CLAUDE.md              # operating contract for AI-assisted work on this repo
├── PRD.md / TRD.md        # what & why / exact contracts — the TRD wins any disagreement
├── Build-Instructions.md  # task-by-task build plan
├── configs/default.yaml   # tunable values; the KEYS are fixed by TRD §1.6
├── src/                   # seven pipeline packages + config loader
├── tests/                 # 212 tests: per-module gates + NFR enforcement
├── notebooks/             # trace-validation plots
└── data/                  # generated at runtime (gitignored)
```

## Scope

This is the **Core Prototype**: a complete local simulation requiring no AWS account and
no paid capacity. Live AWS deployment (Lambda + API Gateway) and training STAR's full
GAT + Transformer + ESRL policy are explicitly Stretch Goals (Phase 9), deliberately out
of scope for "done" — see PRD §2.2 and §2.3.
