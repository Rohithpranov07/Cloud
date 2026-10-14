# Semantic Workload-Composition-Aware Predictive Autoscaling — Project Report (source material)

> **How to use this file.** It is written to be pasted into a chat assistant to produce a full course
> report (BCSE355L, Cloud Architecture Design) and a companion patent disclosure draft. Every number is
> copied from a measured run in this repository; where something was **not** achieved or came out
> unfavourably, it is stated plainly in the same place. Section 12 lists limitations; section 13 lists
> where each claim can be verified. Items marked **[PENDING]** are filled in when the live AWS runs finish.

- **Team:** V Rohith Pranov (24BCE0619), Sudir Niranj R (24BDS0267)
- **Base paper:** Fang, Z., Ma, H., Chen, G., Chen, S. (2026). *STAR: Spatial-Temporal Autoscaling for Cloud
  Applications with Deep Reinforcement Learning.* Expert Systems With Applications, 319, 132105.
- **Repository:** github.com/Rohithpranov07/Cloud (branch `main`)

---

## 1. The claim under test

Cloud autoscalers decide capacity from **aggregate** request rate (and resource metrics). For AI inference
traffic that is a poor signal: requests differ by orders of magnitude in cost and latency depending on their
**semantic archetype**. The claim tested here:

> Conditioning autoscaling **and** cost-governance decisions on the semantic archetype of incoming
> inference requests reduces latency-SLA violations and/or improves cost-budget adherence, compared with an
> archetype-agnostic baseline modelled on the STAR formulation.

The experiment uses a workload that shifts **composition without shifting volume**: the aggregate rate stays
flat while the mix moves from ~10% to ~40% agentic traffic. An aggregate-only autoscaler cannot see that by
construction; the experiment measures what that blindness costs.

**Four fixed archetypes** (never renamed/added/removed): `short_conversational`, `long_context_rag`,
`agentic_tool_using`, `batch_offline`.

**Primitive map** (archetype → AWS serving primitive / pool): `short_conversational → bedrock_on_demand`,
`long_context_rag → sagemaker_endpoint`, `agentic_tool_using → eks_gpu_reserved`, `batch_offline → eks_gpu_spot`.
The baseline uses one `homogeneous_pool`. (In the live system these are *modelled* pools — SQS queues with a
Lambda worker whose concurrency stands for capacity. No GPU/SageMaker/EKS resource is ever created.)

## 2. System design (the pipeline)

Seven modules with a strict one-way import order (a module may import only from earlier ones):

| # | Module | Role |
|---|---|---|
| 1 | `trace_gen` | Synthetic multi-tenant trace, seed-controlled, shift starts at minute 60 |
| 2 | `classifier` | Decision tree labels each request pre-inference from proxy features only |
| 3 | `forecaster` | Per-archetype exponential-smoothing forecast vs. an aggregate-only forecast |
| 4 | `controller` | `baseline_policy` (archetype-agnostic, STAR-style) and `archetype_aware_policy` |
| 5 | `cost_governance` | Projects end-of-window spend; escalates none → downgrade_tier → throttle |
| 6 | `feedback` | Re-fits the classifier on a rolling window (recalibration) |
| 7 | `evaluation` | Head-to-head runs, latency model, multi-seed statistics, sensitivity grid |

Fairness controls (the policies differ **only** in whether they can see composition): same trace, same
forecast horizon, same sizing/rounding rules, same cost table; the baseline's homogeneous pool is priced at a
blended rate *derived* from config (0.04867/unit/min) and a test asserts it lies strictly between the
cheapest and dearest primitive. Budget 24.0 = pre-shift steady-state run rate (22.20) + ~8% headroom.

**Sizing rules.** `round` (the TRD-mandated default) and `ceil` (capacity-correct alternative). Every latency
result is reported under **both** (Addendum §8.1) because under `round` the baseline is already
under-provisioned before the shift, which would credit archetype-awareness with fixing a rounding artefact.

## 3. Part 1 results — simulation of capacity and cost governance (seed 42, rate 50 req/min)

| | pre-shift | post-shift | change |
|---|---:|---:|---:|
| baseline capacity (archetype-agnostic) | 2.20 | 2.05 | −6.8% |
| archetype-aware capacity | 4.00 | 4.77 | +19.2% |

Normalised capacity divergence after the shift: 0.2598. The aware policy scales the *right* primitive
(`eks_gpu_reserved` 1.00 → 1.77; the others stay 1.00); the baseline cannot move because nothing in its input
changes.

**Budget-breach lead time:** aware projects a breach at minute 71 (`downgrade_tier`); aggregate-only at minute
108 → **37 minutes** of lead time (by 108, >20 of the 24 budget units are already spent).

| Module | Gate | Measured |
|---|---|---|
| Trace generator | aggregate std/mean < 0.3 | 0.1363 (agentic share 0.102 → 0.391) |
| Classifier | held-out accuracy > 0.90 | 0.9675 |
| Forecaster | per-archetype detects, aggregate does not | agentic forecast +312% by minute 80; aggregate within 1.3%, never detects |
| Feedback loop | holds/improves on ≥2 of 3 re-fits | 5 of 5 improved |

**Two deviations, recorded not hidden:** (1) forecaster detects at +7 min, not +5 (forcing it earlier raised the
pre-shift noise floor from ~3% to 42–59%); (2) aware controller acts at +11 min not +10, because `eks_gpu_reserved`
serves 10 req/min and `round` withholds a second unit until demand exceeds 15.

## 4. Part 2 — latency model, statistics (offline)

**Latency model** (`latency_model.py`, Addendum §9): discrete-event FIFO queueing per pool, `slots_per_unit = 2`,
base service times 4/8/12/10 s for short/RAG/agentic/batch (derived from unit capacities), SLA = multiplier ×
base service time. Capacity decided at minute *m* applies from the **start** of minute *m* in the simulator
(a one-minute head start over any real controller — this matters in section 8).

Seed-42, rate 50: under `round`, baseline SLA-violation rate ≈ 1.0 in every window (broken before the shift);
aware 0.25 / 0.15 / 0.20 (pre/during/post). Under `ceil`: baseline 0.98 / 0.51 / 0.998; aware 0.25 / 0.007 /
0.009. p95 post-shift: baseline 587 s vs aware 24 s (`ceil`).

**Multi-seed statistics** (30 seeds × 2 sizing rules, paired two-sided Wilcoxon, seeded bootstrap 95% CI,
rank-biserial effect; classifier seed held fixed so only the trace varies). **Rate 50 (the original design):**

| metric (post-shift) | sizing | median baseline | median aware | paired diff [95% CI] | p | r | n |
|---|---|---|---|---|---|---|---|
| SLA-violation rate | round | 1.000 | 0.317 | −0.683 [−0.755, −0.622] | 1.9e-09 | −1.00 | 30 |
| SLA-violation rate | ceil | 1.000 | 0.018 | −0.979 [−0.984, −0.975] | 1.9e-09 | −1.00 | 30 |
| p95 response (s) | round | 2098 | 91.2 | −2011 [−2491, −1376] | 1.9e-09 | −1.00 | 30 |
| p95 response (s) | ceil | 737.9 | 26.6 | −712.6 [−741.3, −674.1] | 1.9e-09 | −1.00 | 30 |
| cost / compliant request | round | undefined | undefined | n=0 | – | – | 0 |
| cost / compliant request | ceil | 0.479 | 0.0066 | −0.473 [−3.607, −0.339] | 2.0e-03 | −1.00 | 10 |

`r = −1.00` means every seed favoured aware. Cost per compliant request under `round` is undefined because the
baseline serves no compliant request post-shift.

**Sensitivity grid** (post-shift agentic share {20,30,40,50}% × SLA multiplier {2,3,5} × sizing, 10 seeds per
cell, 240 runs): every cell positive (aware never worse than baseline within the tested range); under `ceil` at
the default 3× SLA the difference is positive in 10/10 seeds at every share, including 20%. The grid never
tested shares below the pre-shift 10%, so it does not locate a true threshold.

**Same statistics at the live rate (25 req/min)** — required because the live account cannot run rate 50
(section 6). File `docs/multiseed_rate25_summary.json`:

| metric (post-shift) | sizing | median baseline | median aware | paired diff [95% CI] | p | r | n |
|---|---|---|---|---|---|---|---|
| SLA-violation rate | round | 1.000 | 0.277 | −0.723 [−0.765, −0.679] | 1.9e-09 | −1.00 | 30 |
| SLA-violation rate | ceil | 0.273 | 0.036 | −0.248 [−0.318, −0.126] | 1.9e-09 | −1.00 | 30 |
| p95 response (s) | round | 4010 | 97.9 | −3923 [−4014, −3854] | 1.9e-09 | −1.00 | 30 |
| p95 response (s) | ceil | 49.1 | 32.2 | −14.1 [−24.6, −0.43] | 4.7e-03 | −0.58 | 30 |
| cost / compliant request | round | undefined | undefined | n=0 | – | – | 0 |
| cost / compliant request | ceil | 0.00539 | 0.00973 | **+0.00427** [+0.00403, +0.00455] | 3.5e-08 | **+0.97** | 30 |

**Honest reading of the rate-25 result:** at the lower rate the baseline is no longer broken under `ceil`
(its SLA-violation median is 0.27, not 1.0), so the *size* of aware's advantage shrinks sharply (p95 gap 14 s,
effect size −0.58 instead of −1.00). Aware still has fewer violations on every seed, **but it costs more per
compliant request (≈ +80%, significant)**: it holds one unit per pool as a floor, which is idle capacity at low
load. The cost-adherence half of the claim is therefore **not supported at rate 25 under `ceil`**; the latency
half is supported. Under `round` the baseline remains under-provisioned (confound, section 2).

## 5. The AWS system (Part 2)

Region `ap-south-1`; stack `semauto-live` built with AWS SAM (`cloud/template.yaml`), 33 resources, deployed by
the user from their own account; no EC2/SageMaker/EKS/GPU/NAT/provisioned capacity (cost rule C2).

- **Ingress:** HTTP API `POST /classify` → router Lambda (arm64 container image, 1024 MB, 10 s). It classifies
  the request with the same trained model as Part 1 (baked into the image), increments a per-minute counter in
  DynamoDB, and enqueues the request on the pool's SQS queue (`homogeneous_pool` in baseline mode).
- **Pools:** 5 SQS queues, each consumed by a zip worker Lambda (128 MB, 30 s) through an explicit
  `AWS::Lambda::EventSourceMapping` (`BatchSize 1`, `ReportBatchItemFailures`). **Capacity is the mapping's
  `MaximumConcurrency`**, set by the controller: `MaximumConcurrency = max(2, 2 × units)` (2 slots per unit).
  A worker holds its slot for the request's service time (`sleep`) — it models serving, it does not run a model.
- **Controller:** Lambda (same image, 60 s) ticked once per simulated minute by the replay driver (the
  EventBridge schedule exists but is `DISABLED`; determinism matters). It reads counts, runs the same forecaster
  + policy + budget projector as Part 1 (`cloud/adapters.py`), updates the five mappings, and writes a decision
  record. Actuation is therefore a real AWS control-plane operation with real latency.
- **State:** 3 on-demand DynamoDB tables (`counts`, `decisions`, `results`; TTL 7 days), SNS topic + email
  subscription for budget alerts, CloudWatch dashboard. Reference `cloud/` may import `src/`; `src/` never
  imports `cloud/`/boto3 (enforced by a test).
- **Tests:** 467 pass offline with no AWS credentials (botocore Stubber / fakes); `mypy src/` clean.

**Time scale K.** The replay can compress time (arrival and service times ÷ K) so a 2-hour trace takes
2h/K; offered load in slots is unchanged by K. K is chosen by calibration (section 7) and then frozen.

## 6. Environment constraints and how they shaped the design

1. **Lambda concurrency quota = 10** (AWS Support case still open; effective limit verified as 10 via
   `get-account-settings` on 2026-10-14 even though the user reported an increase to 1100 — the request status
   is `CASE_OPENED`, not approved). The spec needs ≥ 50. Peak busy slots at 50 req/min under aware are 10
   (`round`)/12 (`ceil`), which cannot fit. **Fallback (Build-Instructions T11.1): base rate 25 req/min for both
   simulation and live**, everything else unchanged; budget kept at 24.0. This is a documented reduction of
   scale, applied before any live result existed (H1).
2. **Cost ceiling:** the user set a hard ceiling of USD 2 (stricter than the project's USD 5 budget). Spend as
   of the last check: **$0.00** actual; idle stack ≈ $0.002/day (ECR).
3. **Router cold start:** 17–21 s versus the 10 s timeout (container-image init with a heavy model). First request
   after idle returns HTTP 500; the spec fixes 1024 MB/10 s. Mitigation (no template change): the replay driver
   pre-warms several router containers and retries 5xx; every retry is counted and reported.

## 7. Calibration of the live system (T13.2) — what AWS actually does

Measured on the deployed stack (5 repetitions for actuation, 20 probes for the router):

| Measure | p50 | p95 |
|---|---|---|
| Actuation lag, `MaximumConcurrency` 2 → 8 (until 8 are concurrently busy) | 17.9 s | **19.9 s** |
| Actuation lag until the first extra slot | 13.9 s | 15.5 s |
| Router overhead (ingress → enqueue) | 0.09 s | 0.29 s |
| Router round trip | 0.23 s | 0.71 s |
| Warm router handler Duration | 17.9 ms | 24.1 ms |
| Idle SQS→worker polling delay | 0.02 s | 0.04 s |

**Frozen-in-advance rule:** K = largest of {1,2,3,4,6} such that p95 lag < 0.25 × 60 / K (thresholds 15 / 7.5 /
5 / 3.75 / 2.5 s). **Result: no K qualifies** (19.9 s ≥ 15 s even at K=1). Only repetition 1 was uncontaminated
(18.4 s); reps 2–5 started with 8–9 invocations already running because pollers from the previous repetition had
not wound down. Decision by the human: **run all live runs at K=1 (real time) as a documented deviation**, rule
and thresholds unchanged. Consequence: capacity changes lag the decision by ~15–20 s (≈ 30% of a minute), which
the simulator (which applies decisions instantly at the start of the minute) does not model — a known source of
live-vs-simulation divergence, reported rather than corrected.

## 8. Live AWS results (T13.3 / T13.4) — **[PENDING: filled when the four runs finish]**

Design: four runs at K=1, base rate 25, 120 simulated minutes each, order A-B-B-A to cancel drift:
(baseline, ceil), (aware, ceil), (aware, round), (baseline, round). Each run purges the queues, resets pools to
concurrency 2, pre-warms the router, settles 60 s (SQS purge documentation), replays the seed-42 trace in real
time, ticks the controller once per minute, drains, and exports the three tables. Acceptance is on correctness
only (H3): zero lost requests, zero failed controller ticks, p99 send lateness within 0.5 s/K.

**[PENDING] Run table** (sent / 2xx / non-2xx / results rows / router retries / p99 lateness / controller ticks failed)

**[PENDING] Latency table** (live p95, SLA-violation rate by window, baseline vs aware, per sizing rule)

**[PENDING] Live vs simulation** (per-pool capacity, first-breach minute, paired response-time error, governance
lead time, with the "delayed one minute" simulator variant)

**[PENDING] Divergence paragraph** — to be written from the measured numbers, whichever way they point.

Reporting commitments (H1–H3): both sizing rules for every latency claim; no parameter tuned after seeing a
live result; p-values and outcomes are reported as measured and are not acceptance gates.

## 9. Engineering incidents (included because a reviewer will ask)

| Incident | Cause | Resolution |
|---|---|---|
| `simulate_pool` over-admitted slots (5 busy > 4 allowed) | heap forward-search popped entries destructively | rewrote as discrete-event sim with one event queue + FIFO deque |
| Homebrew Python 3.14.8_2 broken on macOS 26.2 (`pyexpat`) | upstream packaging | SAM from official pkg; venv re-pointed to 3.14.7; do not run `brew cleanup` |
| Docker "no space left on device" | disk full | user freed space; restarted Docker |
| Router HTTP 500 on cold start | init > 10 s limit | pre-warm + retry-on-5xx in driver |
| Calibration attempts 1–2 hung 125 s on first Lambda call | stale pooled keep-alive connection after ~4 min idle | fresh client before the phase |
| Calibration attempt 3 lost all measurements; left a pool at 8 | local errno 49 on the final reset call | persist each rep; retry the restore (restored by hand, verified) |
| Live run 1 (first try) invalid: ticks 10 and 113–119 failed, driver crashed in drain | laptop network/DNS loss | retry only never-delivered errors (a tick can never run twice); drain tolerates errors; driver files written before drain; any failed tick → exit 3 INVALID; invalid runs quarantined and retried once |

Spec conflicts surfaced instead of silently chosen: task said sleep 30 s after queue purge (docs say 60 s → docs
win); task said first controller tick at minute 10 (nothing changes then → minute 70 used for the actuation smoke
test); simulator vs live decision timing (reported both ways); live baseline governance is judged on its own
spend, whereas `run_comparison` projects the baseline from aware's spend.

## 10. Cost and safety

Budget `semauto` USD 5 with alerts; hard personal ceiling USD 2; no account IDs/ARNs/keys/emails committed; no
AWS call from any test; no resource creation without explicit approval until the user's final instruction to
proceed autonomously; stack teardown (all three stacks + ECR images + log groups) at the end. **[PENDING] final
spend after teardown.**

## 11. Contributions suitable for a patent disclosure (claims candidates)

1. Using a pre-inference **semantic archetype label** of each request as the conditioning variable for
   autoscaling decisions, with an independent forecast and sizing rule per archetype.
2. Routing each archetype to a **distinct serving primitive** (on-demand API, managed endpoint, reserved GPU,
   spot GPU) and sizing each pool separately, versus a single homogeneous pool.
3. **Cost governance conditioned on the same archetype forecast**: projecting end-of-window spend per archetype and
   escalating none → downgrade tier → throttle earlier (measured 37-minute lead time at rate 50).
4. A **feedback loop** that recalibrates the classifier on rolling telemetry (5/5 checkpoints improved).
5. A **reduction to practice** on real AWS control-plane primitives (SQS + Lambda event-source-mapping
   concurrency as the actuated capacity), including measured actuation latency (section 7).

Evidence strength, stated honestly: the latency advantage is statistically robust in simulation across 30 seeds at
the original rate; at the lower live rate the cost-per-compliant-request comparison **favours the baseline**, and
the live runs are **[PENDING]**.

## 12. Limitations

- Workload is synthetic (seed-controlled trace); archetype labels come from proxy features of that generator.
- Pools are **modelled** (sleep-based workers), not real GPU/Bedrock/SageMaker/EKS serving; costs are table-based.
- Lambda quota 10 forced rate 25 (half the designed load); the original-rate result exists only in simulation.
- K=1 live runs violate the calibration rule (actuation lag 15–20 s ≈ 30% of the decision interval); the simulator
  has a one-minute head start; both are reported as divergence sources.
- Router cold starts (17–21 s) required pre-warming and retries; retry counts are reported.
- Forecast detection at +7 min and controller action at +11 min deviate from TRD gates by design (section 3).
- `round` confounds the baseline; `ceil` is the cleaner test; both are always reported.
- One trace seed live (42); statistics come from simulation only.
- Single region, single account, no multi-tenant contention, no real network variability beyond one laptop.
- The laptop network was unreliable during live runs (documented incidents); invalid runs are quarantined in
  `data/live/invalid/` and not analysed.

## 13. Where to verify

`README.md` (setup, Part 1 and Part 2 results), `docs/aws_live_notes.md` (running AWS log, every command outcome),
`docs/multiseed_rate25_summary.json`, `data/multiseed_summary.json`, `data/latency_summary.json`,
`data/sensitivity.csv`, `data/live/*` (gitignored; run CSVs, `summary.json`, plots), `TRD.md` and
`TRD-AWS-Addendum.md` (fixed specifications), `CLAUDE.md` (operating contract). Reproduce Part 1 with
`python -m src.evaluation.run_comparison`; tests with `pytest` (467) and `mypy src/`.
