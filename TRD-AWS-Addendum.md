# TRD Addendum — Latency Evaluation, Statistical Evaluation & Live AWS Integration
### Technical Requirements Document, §8–§14 — extends `TRD.md` §0–§7

> **Status of this document.** `TRD.md` §0–§7 remain in force, unchanged, for everything they cover. This addendum adds the specification for the work that takes the project from "Core Prototype done" (Phases 0–8) to "100% of the PRD": the latency/SLA evaluation the PRD's primary metric asks for, the statistical evidence for it, and a live AWS deployment of the control plane. It plays the same role for `Build-Instructions-Part2.md` that `TRD.md` plays for `Build-Instructions.md`: **if a Part 2 prompt seems to disagree with this addendum, this addendum wins.** If this addendum seems to disagree with `TRD.md` §1–§2 on anything already built, `TRD.md` wins and the conflict is raised with a human.
>
> It supersedes `TRD.md` §7's non-goal "Any live AWS deployment" and `Build-Instructions.md` T9.2 (the classifier-only Lambda demo). T9.1 (the AWS Budgets safety net) is kept and reused as T11.1.

---

## 8. Why this addendum exists — the three gaps in the Core Prototype

| # | Gap | Evidence | What closes it |
|---|---|---|---|
| G1 | **Latency is never measured.** PRD §8's *primary* metric is mean response time / SLA violations. The Core Prototype records capacity and spend only. | `evaluation_results.csv` has no latency column. | §9 — a request-level queueing model (simulation) and §11 — real queueing latency measured on AWS. |
| G2 | **Single seed, no significance test.** PRD §8 asks for a *statistically significant* difference. | Every figure comes from seed 42. | §10 — 30-seed paired evaluation with a Wilcoxon signed-rank test. |
| G3 | **Nothing runs on AWS.** PRD §2.2 stretch goal; the patent and report describe an AWS architecture. | No deployed component. | §11 — serverless control plane on AWS, with emulated serving pools, torn down after. |

### 8.1 A finding that shapes the latency work (measured, not assumed)

In the verified seed-42 run, the baseline holds **2 units for 97 of 110 minutes** (`baseline_capacity` value counts: `{2: 97, 3: 13}`). With `per_unit_capacity = 20`, that is 40 req/min of nominal capacity against an aggregate demand of about 50 req/min. The cause is the TRD §2 sizing rule `round(predicted / per_unit)`: the smoothed forecast sits near 49, and `round(2.45) = 2`.

This never showed up because capacity was never turned into latency. Once it is, the baseline will look overloaded **before the shift as well as after it**. That would credit archetype awareness with an effect that really comes from the rounding rule. This is the same confound the code comment in `archetype_aware_policy.py` flags as "needs a human decision". The addendum settles it like this:

- **The Core Prototype stays as it is.** `sizing_rule: round` remains the default, and the headline numbers in `README.md` and the final report do not change.
- **Every latency result is reported under BOTH sizing rules,** each applied to both policies: `round` (TRD-faithful) and `ceil` (correct for a capacity controller). The `ceil` run is the clean test of archetype awareness. The `round` run is reported next to it, with this explanation.
- **Latency claims rest on the post-shift window and on the change from pre- to post-shift within each policy,** never on the pre-shift level alone.

---

## 9. Latency model (simulation) — closes G1 in simulation

### 9.1 Physical model (fixed)

Every capacity unit is backed by **`slots_per_unit = 2`** concurrent servers ("slots"). The value 2 is not arbitrary: it is the minimum `MaximumConcurrency` of a Lambda SQS event-source mapping (range 2–1000). The simulated and live systems therefore use the *same* unit→slot mapping.

**Base service time per archetype** is *derived* from the existing TRD §1.6 tables, never configured separately. A unit of primitive *p* serves κ_p req/min with 2 slots, so each request occupies a slot for:

```
base_service_s[a] = slots_per_unit * 60 / PRIMITIVE_UNIT_CAPACITY[PRIMITIVE_MAP[a]]
→ short_conversational 4.0 s · long_context_rag 8.0 s · agentic_tool_using 12.0 s · batch_offline 10.0 s
```

**Per-request service time** reuses the noise the trace generator already draws, so no new randomness is introduced:

```
service_s[i] = base_service_s[true_archetype_i] * compute_cost_i / COMPUTE_COST_BASE[true_archetype_i]
```

Service time depends on the **true** archetype, because that is the real work. Routing uses the **predicted** archetype, because that is what the system knows. A misclassified request lands in the wrong pool and still costs its true service time, exactly as it would in production.

**Baseline unit, honestly modelled.** A homogeneous unit is also 2 slots. Its real throughput depends on the mix: 2·60 / E[service_s] ≈ **18.5 req/min** at the pre-shift mix and ≈ **13.5 req/min** at the post-shift mix, while the baseline *believes* every unit gives 20 req/min. That gap is the mechanism the project claims, written into the physics rather than asserted.

**Arrival times within a minute:** `arrival_s[i] = minute_i * 60 + U[0, 60)`, drawn with `np.random.default_rng(latency.arrival_seed)` in `request_id` order. The live replay uses exactly the same offsets.

**Queue discipline:** one FIFO queue per pool. The aware policy has 4 pools (one per primitive). The baseline has 1 pool (`homogeneous_pool`). At time *t* the number of busy slots in a pool may not exceed `slots_per_unit × units(pool, minute(t))`. When a pool scales down, running requests finish and nothing is pre-empted.

**Capacity before the first decision** (minutes 0 to `start_minute − 1`): baseline 1 unit; aware 1 unit per primitive. Both the simulation and the live replay use these values.

**SLA per archetype** = `sla_multiplier[a] × base_service_s[a]`. The defaults are fixed **before any latency result is seen** and live in `configs/default.yaml`:

| archetype | multiplier | SLA |
|---|---|---|
| short_conversational | 3 | 12 s |
| long_context_rag | 3 | 24 s |
| agentic_tool_using | 3 | 36 s |
| batch_offline | 10 | 100 s (latency-insensitive) |

Sensitivity to these choices is reported (T10.4); the choices are never tuned to make a result look better.

### 9.2 Config additions (`configs/default.yaml`)

Keys are fixed. Values are tunable.

```yaml
evaluation:
  sizing_rule: round          # round | ceil — applied to BOTH policies; default keeps TRD §2 behaviour
latency:
  slots_per_unit: 2
  arrival_seed: 7
  initial_units_baseline: 1
  initial_units_aware_per_primitive: 1
  sla_multiplier:
    short_conversational: 3
    long_context_rag: 3
    agentic_tool_using: 3
    batch_offline: 10
multiseed:
  seeds: [0, 1, 2, ..., 29]   # write all 30 explicitly
  classifier_seed: 0           # held fixed so only the trace varies
```

`src/config.py` validation is extended: `sizing_rule ∈ {round, ceil}`; `sla_multiplier` keys must be exactly the archetypes; `slots_per_unit ≥ 2`.

### 9.3 Contract changes (keyword-compatible only)

```python
# src/controller/baseline_policy.py
def baseline_scaling_decision(aggregate_forecast, current_capacity: int,
                              per_unit_capacity: int = 20, sizing: str = "round") -> dict: ...
# src/controller/archetype_aware_policy.py
def archetype_aware_scaling_decision(per_archetype_forecast: dict, current_capacity: dict,
                                     sizing: str = "round") -> list[dict]: ...
```

With `sizing="round"` (the default), outputs must be **byte-identical** to the current implementation. A regression test against a golden copy of `data/evaluation_results.csv` taken before the change proves it. `ceil` uses `max(1, math.ceil(demand / unit_capacity))`, with the same zero-demand rule as before.

### 9.4 New module `src/evaluation/latency_model.py`

```python
BASE_SERVICE_S: dict[str, float]        # derived at import from config, per §9.1 — not a second table

def service_seconds(df: pd.DataFrame) -> pd.Series: ...
    # per-request service time, §9.1; needs true_archetype, compute_cost
def arrival_seconds(df: pd.DataFrame, seed: int) -> pd.Series: ...
    # minute*60 + U[0,60), in request_id order
def simulate_pool(arrival_s: np.ndarray, service_s: np.ndarray,
                  slots_by_minute: Mapping[int, int]) -> pd.DataFrame: ...
    # FIFO multi-slot queue; returns start_s, end_s, wait_s, response_s aligned to inputs
def capacity_timeline(results: pd.DataFrame, policy: Literal["baseline", "aware"],
                      duration_minutes: int) -> dict[str, dict[int, int]]: ...
    # pool -> {minute -> units}, including the §9.1 initial capacity
def simulate_policy_latency(labeled: pd.DataFrame, results: pd.DataFrame,
                            policy: Literal["baseline", "aware"], seed: int) -> pd.DataFrame: ...
    # one row per request: request_id, minute, true_archetype, predicted_archetype, pool,
    # arrival_s, service_s, wait_s, response_s, sla_s, sla_violated
def summarise_latency(per_request: pd.DataFrame, shift_start: int, shift_end: int) -> dict: ...
    # mean/p50/p95/p99 response and SLA-violation rate: overall, per archetype, and per window
    # (pre | during | post)
```

Import direction: `latency_model` may import from `src.config`, `src.trace_gen` (for `COMPUTE_COST_BASE`) and nothing later than `src.evaluation`.

### 9.5 `run_comparison` refactor and new columns

- Extract the minute loop into `simulate(cfg: TraceConfig, sizing: str = "round") -> tuple[pd.DataFrame, pd.DataFrame]`, which returns `(results, labeled)` and writes no files. `run()` calls it, writes the same two files plus the latency outputs, and prints the same summary plus a latency section.
- Each policy's realised spend is now recorded separately: `baseline_actual_spend` (units × blended unit cost) and `aware_actual_spend` (the current `cumulative_actual_spend`, which stays under its existing name). The existing projection logic does not change.
- New outputs:
  - `data/latency_per_request_{policy}_{sizing}.csv`
  - `data/latency_summary.json` (both policies × both sizing rules)
  - `data/latency_comparison.png`
- The five TRD §1.8 `EvaluationRow` columns are untouched.

---

## 10. Statistical evaluation — closes G2

### 10.1 New module `src/evaluation/multiseed.py`

```python
def run_seed(seed: int, sizing: str) -> dict: ...  # one row of metrics for one trace seed
def run_all(seeds: list[int], sizings: tuple[str, ...] = ("round", "ceil"),
            workers: int = 1) -> pd.DataFrame: ...
def paired_tests(df: pd.DataFrame) -> dict: ...    # scipy.stats.wilcoxon, per sizing rule
def main() -> None: ...  # writes data/multiseed_results.csv, data/multiseed_summary.json,
                         # data/multiseed_distributions.png
```

**Per-seed metrics** (all from functions that already exist, none re-derived by hand):

- `detect_minute`: first minute ≥ `shift_start` at which the agentic forecast exceeds its pre-shift mean by more than 25%. This is the same rule as `test_per_archetype_detects_the_shift_before_the_aggregate_does`, moved into a shared helper so the test and the module use one definition.
- `aware_scaleup_minute`: first minute with `aware_capacity_eks_gpu_reserved ≥ 2`.
- `aware_breach_minute`, `baseline_breach_minute`, `governance_lead_min`. The baseline may never breach; record that as NaN, never as 0.
- Per policy, post-shift window (minute ≥ `shift_start + shift_duration`): `p95_response_s`, `sla_violation_rate`, `actual_spend`, `cost_per_compliant_request = actual_spend / n_requests_within_SLA`.

**Tests** (paired across seeds, two-sided, `scipy.stats.wilcoxon`): baseline vs aware on `sla_violation_rate`, `p95_response_s` and `cost_per_compliant_request`, run separately for each sizing rule. For each test, report n, the median paired difference, its bootstrap 95% CI (10,000 resamples, seeded), the p-value, and the matched-pairs rank-biserial effect size. **No test outcome is a gate.** Whatever the numbers are, they are reported.

### 10.2 Sensitivity grid (T10.4, `src/evaluation/sensitivity.py`)

The grid varies the post-shift agentic share ∈ {0.20, 0.30, 0.40, 0.50}, with `short_conversational` absorbing the difference so the mix still sums to 1; `sla_multiplier` (interactive archetypes) ∈ {2, 3, 5}; and `sizing` ∈ {round, ceil}. It uses 10 seeds per cell. Outputs: `data/sensitivity.csv` and `data/sensitivity_heatmap.png`. Its question is how big a composition shift must be before archetype awareness pays for itself.

---

## 11. Live AWS integration — closes G3

### 11.1 What is real and what is emulated (state this in the report verbatim)

| Report architecture component | Live on AWS | How |
|---|---|---|
| Ingress + semantic classifier | **Real** | API Gateway HTTP API → Lambda (container image) running the unchanged `src.classifier` code |
| Per-archetype telemetry | **Real** | DynamoDB atomic per-minute counters + CloudWatch metrics (Embedded Metric Format) |
| Per-archetype forecaster | **Real** | Controller Lambda running the unchanged `src.forecaster` code |
| Archetype-aware / baseline controller | **Real** | The same Lambda, running the unchanged `src.controller` code |
| Capacity actuation | **Real** | `UpdateEventSourceMapping` → `ScalingConfig.MaximumConcurrency` = `slots_per_unit × units` per pool |
| Serving primitives (Bedrock / SageMaker / EKS-GPU reserved / EKS-GPU spot) | **Emulated** | One SQS queue + one worker Lambda per primitive. The worker holds a slot for the request's `service_s` (as in §9.1) and does no inference. Queueing delay is therefore *real* infrastructure behaviour; GPU inference is not. |
| Cost governance | **Real logic, simulated prices** | `src.cost_governance` runs in the controller on TRD §1.6 unit prices and publishes to SNS on a projected breach. The real AWS bill is guarded separately by AWS Budgets. |
| Feedback / recalibration | **Not deployed** | It needs post-inference ground-truth labels, which an emulated pool cannot produce honestly. It stays simulated (Phase 6) and is listed as future work. |
| Full STAR GAT+Transformer+ESRL policy | **Not built** | Out of scope (PRD §9 risk table); future work. |

### 11.2 Repo layout additions (fixed)

```
cloud/                         # NEW top-level package. May import from src/. src/ NEVER imports cloud/ or boto3.
├── __init__.py
├── Dockerfile                 # one image for router + controller (FROM public.ecr.aws/lambda/python:3.14, arm64)
├── requirements-lambda.txt    # runtime subset of requirements.txt, same pins + boto3
├── build_model.py             # trains the classifier at image-build time → /var/task/model/classifier.joblib
├── common.py                  # env/config loading, EMF helper, DynamoDB/SQS/Lambda client factories
├── handlers/
│   ├── router.py              # HTTP API → classify → count → enqueue
│   ├── controller.py          # tick → forecast → decide → actuate → govern → record
│   └── worker.py              # SQS → hold slot for service_s / time_scale → record result (zip, stdlib + boto3 only)
├── replay.py                  # local driver: replays the trace against the live stack, ticks the controller
├── analyze_live.py            # pulls results, computes live latency, compares against §9 simulation
├── template.yaml              # AWS SAM template — the ONLY place infrastructure is defined
└── samconfig.toml             # region ap-south-1, stack name semauto-live, tags
requirements-cloud.txt         # local dev: boto3 (+ botocore Stubber comes with it)
tests/cloud/                   # handler unit tests with botocore.stub.Stubber — NO network, NO real AWS
docs/aws_live_notes.md         # budget, quota, deploy, run, teardown log + screenshot index
data/live/                     # live run outputs (gitignored except .gitkeep)
```

### 11.3 Data models (fixed field names)

**Ingress request body** (JSON, `POST /classify`):

| field | type | notes |
|---|---|---|
| `run_id` | str | `"{policy}-{sizing}-{yyyymmddHHMMSS}"` |
| `policy` | `"baseline"` \| `"aware"` | selects the routing table |
| `request_id` | int | from the trace |
| `minute` | int | **simulated** minute (from the trace, not the wall clock), so the forecaster sees identical minute indexing |
| `declared_output_len`, `has_tool_schema`, `prompt_domain_score` | as TRD §1.2 | the only classifier inputs |
| `service_s` | float | from §9.1; carried, never read by the classifier |
| `true_archetype` | str | carried to the result row for **evaluation only**; the router must never read it (test-enforced) |

**Router response** (`202`): `{"request_id", "predicted_archetype", "confidence", "pool"}`.

**SQS message body**: ingress body + `predicted_archetype`, `confidence`, `pool`, `ingress_ts` (router wall-clock epoch seconds, float).

**DynamoDB tables** (on-demand billing, TTL attribute `expires_at` = now + 7 days):

| table | PK | SK | attributes |
|---|---|---|---|
| `semauto-counts` | `run_id` (S) | `minute` (N) | one Number attribute per archetype, incremented with `UpdateItem ... ADD` |
| `semauto-decisions` | `run_id` (S) | `minute` (N) | `policy`, `sizing`, `capacity` (Map pool→units), `forecast` (Map archetype→peak), `projected_spend`, `budget_action`, `breach_projected`, `actuated` (Map pool→MaximumConcurrency), `tick_ts` |
| `semauto-results` | `run_id` (S) | `request_id` (N) | `pool`, `true_archetype`, `predicted_archetype`, `service_s`, `ingress_ts`, `start_ts`, `end_ts` |

**Live response time** = `end_ts − ingress_ts`, in seconds. Both timestamps are AWS clocks, so the laptop's clock skew never enters. It is converted to *simulated seconds* by multiplying by `time_scale`.

### 11.4 Handler contracts

```python
# cloud/handlers/router.py
def handler(event: dict, context: object) -> dict: ...
    # API Gateway HTTP API payload v2.0 → response dict (statusCode, headers, body)
# cloud/handlers/controller.py
def handler(event: dict, context: object) -> dict: ...
    # event {"run_id","policy","sizing","minute"} | {"warmup": true} | {"reset": {"policy","run_id"}}
# cloud/handlers/worker.py
def handler(event: dict, context: object) -> dict: ...
    # SQS batch (batch size 1) → {"batchItemFailures": [...]}
```

Each handler keeps its logic in pure functions that take injected clients, for example `route(body, clf, ddb, sqs, queue_urls, now)`. Unit tests call those functions with `Stubber`-wrapped clients. The `handler` wrappers stay thin.

**Controller actuation rule:** `MaximumConcurrency = max(2, slots_per_unit × units)`, applied only when the value changes. A pool with 0 required units is held at the floor of 2 and logged, because an ESM cannot go below 2. The controller is stateless: it reads its previous capacity from the latest `semauto-decisions` item for the run.

**Governance notification:** SNS publish **once per run**, on the first minute where `breach_projected` turns true. The message includes the action and the overage. Enforcement (deferring `batch_offline` under `throttle`) is optional (T12.8) and off by default (`ENFORCE_GOVERNANCE=false`). Every reported live result states whether it was on.

### 11.5 Infrastructure (SAM, `cloud/template.yaml`)

- **Region** `ap-south-1`. **Architecture** `arm64` for all functions. **Global tag** `project=semantic-autoscaling` on every taggable resource.
- **HttpApi**, with a single route `POST /classify` → `RouterFunction` (PackageType Image, 1024 MB, timeout 10 s).
- **ControllerFunction** (same image, different `ImageConfig.Command`; 1024 MB, timeout 60 s). There is **no API route** for it; only the replay driver (IAM) invokes it. An optional `ScheduleV2` rate(1 minute) event is declared with `State: DISABLED` for "live mode".
- **5 queues:** `bedrock_on_demand`, `sagemaker_endpoint`, `eks_gpu_reserved`, `eks_gpu_spot`, `homogeneous_pool`. Each queue's visibility timeout is at least 6× its worker's timeout.
- **5 WorkerFunctions** (zip, `python3.14`, 128 MB, timeout = ceil(max service_s) + 10 s). Each has an explicit `AWS::Lambda::EventSourceMapping` with `BatchSize: 1` and `ScalingConfig.MaximumConcurrency: 2`. `Ref` on the mapping returns its UUID, which is passed to the controller as an environment variable. **No reserved concurrency** is set on any function (see §12 NFR-A3).
- **3 DynamoDB tables** (§11.3), one **SNS topic** with an email subscription parameter, and one **CloudWatch dashboard**.
- **IAM:** least privilege per function. The router may `dynamodb:UpdateItem` on counts and `sqs:SendMessage` on the 5 queues. The controller may Query/PutItem decisions, Query counts, `lambda:UpdateEventSourceMapping` on the 5 mappings, and `sns:Publish`. The worker may PutItem on results.

### 11.6 Replay protocol (`cloud/replay.py`)

1. Load the seed-42 trace with the same functions and seeds as `simulate()` (`generate_trace` → `add_proxy_features(seed=cfg.seed)` → `service_seconds` → `arrival_seconds(seed=latency.arrival_seed)`). `cloud/build_model.py` trains the router's classifier with `seed=cfg.seed` for the same reason.
2. For each run: generate a `run_id` → `PurgeQueue` on all 5 queues → invoke the controller with `{"reset": ...}` (sets every ESM to the §9.1 initial capacity) → `{"warmup": true}` → wait 30 s.
3. Send each request at wall time `t0 + arrival_s / time_scale` (thread pool, stdlib `urllib`). At each simulated-minute boundary ≥ `start_minute`, invoke the controller **synchronously** with that minute.
4. After the last minute, wait until all 5 queues report 0 visible and 0 in-flight messages (timeout 15 min). Then export the three tables for the `run_id` to `data/live/{run_id}_{table}.csv`.
5. Record the driver log (send times, HTTP status, RTT) to `data/live/{run_id}_driver.csv`.

`time_scale` (default 1) divides arrival times and service times alike, so concurrency demand matches real time. It is chosen in T13.2 from the measured actuation lag and may not exceed the largest value that keeps the p95 actuation lag below 25% of a simulated minute.

---

## 12. Non-functional requirements (additions)

- **NFR-A1 Cost ceiling.** An AWS Budgets budget of **USD 5** exists *before* the first `sam deploy`, with alerts at 50%, 80% and 100% of actual spend and 100% of forecast spend. Expected total spend for Phases 11–14 is **under USD 2**; the AWS Billing console is the source of truth.
- **NFR-A2 Teardown.** Every deployed resource is deleted in the same working session it was created in (`sam delete`, plus the SAM-managed ECR repository and the log groups). A tag-based resource query showing zero resources is the proof.
- **NFR-A3 Concurrency quota.** The account's Lambda concurrent-executions quota in `ap-south-1` must be **≥ 50** before T12.6. New accounts can start at 10, and with 10 the worker pools (up to 10 slots) and the router would compete. Request an increase through Service Quotas. If it is not granted, follow the documented fallback in T11.1.
- **NFR-A4 No real AWS in tests.** `pytest` must pass with no network and no credentials. Any test that would reach AWS is a bug.
- **NFR-A5 One source of truth.** The Lambdas import `src/` unchanged. No second copy of `ARCHETYPES`, the cost tables, the sizing rule, the forecaster or the classifier may appear under `cloud/`. The router loads the classifier trained by `src.classifier.train_classifier` at image-build time.
- **NFR-A6 Secrets.** No account IDs, ARNs, emails or keys are committed. The SNS email is a `sam deploy` parameter, and `samconfig.toml` holds no secrets.
- **NFR-A7 Determinism where possible.** Everything up to the AWS boundary is seeded. The live run's non-determinism (cold starts, polling delay, scheduling jitter) is *measured and reported*, never smoothed away.

---

## 13. Acceptance gates (additions)

| Area | Gate |
|---|---|
| Sizing refactor | `sizing="round"` reproduces the golden `evaluation_results.csv` byte-for-byte; `ceil` has its own table-driven tests. |
| Latency model | (a) Conservation: every request has `arrival ≤ start ≤ end` and none are lost. (b) Busy slots never exceed the allowed slots at any instant. (c) Deterministic arrivals and service at one slot match the hand-computed waits exactly. (d) An M/M/1 run with its own seeded rng (ρ = 0.5, n ≥ 20,000) has a mean wait within 5% of ρ/(μ−λ). (e) Same seed gives identical output. |
| Multi-seed | 30 seeds × 2 sizing rules complete. `multiseed_summary.json` carries n, the median paired difference, the bootstrap CI, p and the effect size for all three metrics. The detection helper is shared with the forecaster test (one definition). |
| Cloud unit tests | `pytest tests/cloud` passes offline. The router test proves `true_archetype` is never read (scrambling it changes nothing). The controller test proves `MaximumConcurrency = max(2, 2·units)` and that it changes only on a delta. |
| Image parity | Inside the built image, the router's predictions on the seed-42 trace equal the local `classify()` predictions (100% agreement). |
| Live deploy | A real `curl` to `/classify` returns `202` with a valid `predicted_archetype`. One controller tick changes an ESM's `MaximumConcurrency` (shown by `aws lambda get-event-source-mapping`). |
| Live run | For every run, `results` row count = sent count (zero lost requests), and every decision with a change has a matching actuation record. |
| Live vs sim | Reported for every run, not gated: live p50/p95 per pool next to the §9 simulation on the same trace, capacity timeline and sizing rule, with the absolute and relative error. |
| Teardown | `aws resourcegroupstaggingapi get-resources --tag-filters Key=project,Values=semantic-autoscaling --region ap-south-1` returns an empty list. The final cost is recorded from the Billing console. |

---

## 14. Explicit non-goals (still out of scope)

- Real GPU inference, real SageMaker endpoints, or real EKS clusters. Their cost profile breaks NFR-A1, and emulated pools test the control-plane claim fully.
- Training the full STAR GAT+Transformer+ESRL policy.
- Deploying the recalibration loop (§11.1).
- Multi-region or multi-account deployment, or a production hardening review (WAF, auth on the API beyond IAM-free demo use). The API is public only while a run is in progress and is deleted afterwards.
