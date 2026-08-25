# PRD — Semantic Workload-Composition-Aware Predictive Autoscaling & Cost-Governance Architecture on AWS
### Product/Project Requirements Document — Core Prototype

> Companion to `TRD.md` (technical spec) and `Build-Instructions.md` (Claude Code task prompts). This document answers *what* to build and *why*, and *when*; the TRD answers *exactly how* each piece is shaped. If this PRD and the TRD ever disagree on a technical detail (a field name, a function signature), **the TRD wins** — this document should be updated to match, not the other way around.

**Course:** BCSE355L — Cloud Architecture Design · **Team:** V Rohith Pranov (24BCE0619), Sudir Niranj R (24BDS0267) · **Base Paper:** Fang, Z., Ma, H., Chen, G., & Chen, S. (2026). *STAR: Spatial-Temporal Autoscaling for Cloud Applications with Deep Reinforcement Learning.* Expert Systems With Applications, 319, 132105.

---

## 0. Reality Check Before You Start

Read this section first — it sets the scope for everything below. The architecture in the course report's Section 4 is a full production system: a trained deep-RL policy (GAT + Transformer, per STAR), five AWS service layers, and a live multi-tenant cost-governance loop. Built and evaluated properly, that is months of work for a funded infrastructure team, not a two-person course project. Attempting the full production version in the time available risks a half-working demo of everything instead of a fully-working demo of the core idea.

This PRD scopes the project into a **Core Prototype** that is realistically buildable and defensible in an evaluation, plus clearly marked **Stretch Goals** attempted only if the core finishes early. The Core Prototype keeps the part of the architecture that is actually novel — archetype-conditioned scaling decisions, evaluated against an archetype-agnostic baseline — and simplifies the parts that are expensive but not where the novelty lives (training a full GAT+Transformer RL policy from scratch, running a live multi-tenant AWS bill for weeks).

---

## 1. Objective

Build and evaluate a working prototype that demonstrates the central claim of the report: that conditioning autoscaling and cost-governance decisions on the semantic archetype of incoming requests (not just aggregate request rate) improves latency-SLA adherence and/or cost efficiency relative to an archetype-agnostic baseline modeled on STAR's original formulation.

---

## 2. Goals and Non-Goals

### 2.1 Goals (Core Prototype)

- A synthetic request-trace generator producing labeled requests across four archetypes (short-conversational, long-context-RAG, agentic-tool-using, batch-offline) with distinct compute-per-request profiles.
- A working Semantic Archetype Classifier (rule-based or a small trained classifier — not necessarily an LLM call) that labels each request pre-inference.
- Per-archetype demand forecasting (a lightweight statistical model is sufficient — exponential smoothing or a small gradient-boosted model).
- A capacity-controller policy that makes archetype-aware scaling decisions, and a baseline archetype-agnostic policy (approximating STAR's original formulation) to compare against.
- A cost-governance layer that projects per-tenant spend from the forecast and triggers a throttle/downgrade action before a simulated budget breach.
- An evaluation comparing archetype-aware vs. archetype-agnostic policies on mean response time, cost-budget adherence, and detection lead time for compositional shifts.

### 2.2 Goals (Stretch — attempt only if core finishes early)

- Replace the statistical forecaster and heuristic capacity controller with an actual small-scale GAT + Transformer policy trained via evolutionary-strategy RL, closer to STAR's full method.
- Deploy the ingress/classification and cost-governance layers as real AWS Lambda + API Gateway functions (free-tier eligible) fronting a small, real EKS or SageMaker endpoint, instead of a pure simulation.
- Live dashboard (CloudWatch or a small Grafana/Streamlit panel) showing forecast vs. actual archetype mix in real time.

### 2.3 Explicit Non-Goals (do not attempt in this course project)

- A production-grade, multi-tenant, live-traffic deployment with real paying tenants or real GPU workloads.
- Training STAR's full policy on the same NASA/Wiki/Ali traces at the same scale as the base paper — use a smaller, representative slice instead.
- Filing or relying on the companion patent disclosure for grading purposes — that is a separate track and shouldn't gate the course deliverable.

---

## 3. Users and Evaluation Context

- **Primary evaluator:** course faculty (Suganthini C), assessing architecture understanding, implementation correctness, and whether the evaluation supports the report's claims.
- **Secondary audience:** the team itself — this prototype also serves as the reduction-to-practice evidence recommended in the companion patent disclosure's "Recommended Next Steps."

---

## 4. Functional Requirements

| Layer | Requirement | Acceptance Check |
|---|---|---|
| Trace Generator | Produce a request stream with configurable archetype mix and compositional shift events (e.g., agentic share rising from 10% to 40% over 10 minutes at constant aggregate rate). | A shift event is present in the trace and is NOT visible in the aggregate request-count time series alone. |
| Classifier | Label each request with an archetype and confidence score using pre-inference features only. | >90% labeling accuracy against the trace generator's ground truth on held-out data. |
| Forecaster | Maintain an independent short-horizon forecast per archetype, updated on a fixed interval. | Forecast error (MAE/RMSE) reported per archetype; forecast detects the shift event before it appears in the aggregate signal. |
| Capacity Controller | Archetype-aware policy selects a target pool/primitive and scaling action; baseline archetype-agnostic policy makes the same decision without archetype input. | Both policies run on the identical trace; decisions and resulting simulated latency are logged for comparison. |
| Cost Governance | Project per-tenant spend from the forecast and trigger a policy action before a simulated budget threshold is crossed. | Budget breach is caught with positive lead time in the archetype-aware run; measure how much lead time is lost in an aggregate-only run. |
| Feedback Loop | Recalibrate classifier/forecaster from simulated post-inference telemetry on a rolling basis. | Classifier accuracy or forecast error measurably improves over the course of a multi-hour simulated run. |

> Precise data shapes, function signatures, and file locations for each of these six layers are fixed in `TRD.md` §1–§2, and built task-by-task in `Build-Instructions.md` §D Phases 1–6.

---

## 5. Non-Functional Requirements

- **Reproducibility:** the entire Core Prototype must run end-to-end from a single script/notebook on a laptop or a single small AWS free-tier account — no dependency on paid GPU capacity for the core evaluation.
- **Cost ceiling:** total AWS spend across the whole project should stay within free-tier limits or a small fixed budget set in advance (e.g., under ₹2,000 / ~$25) — track this explicitly, since a cost-governance project going over its own budget is an obvious and avoidable credibility problem.
- **Traceability:** every claim in the final report's Evaluation section must point to a specific logged run and script, not to a hand-picked or unlogged result.

> These map 1:1 to `TRD.md` §5 (NFR-1 through NFR-5), which restates each as a testable gate.

---

## 6. Milestones and Timeline

Assuming roughly 8–10 weeks remain before final submission; adjust dates to your actual deadline.

| Week | Milestone | Exit Criteria |
|---|---|---|
| 1–2 | Trace generator + archetype schema | Synthetic trace with 4 archetypes and at least one compositional-shift scenario, saved as a reusable dataset. |
| 3 | Classifier | Rule-based or lightly-trained classifier hits >90% accuracy on held-out trace data. |
| 4–5 | Forecaster + baseline STAR-style policy | Per-archetype forecaster running; archetype-agnostic baseline policy implemented and producing scaling decisions on the trace. |
| 6–7 | Archetype-aware capacity controller + cost governance | Both layers implemented; head-to-head run against baseline completed and logged. |
| 8 | Feedback loop + evaluation writeup | Recalibration loop shown to improve accuracy/forecast error over a run; metrics tables and plots drafted. |
| 9–10 | Buffer / stretch goals / report polish | Core report finalized first; stretch items (live AWS deployment, full STAR RL policy) attempted only if time remains. |

> This same timeline is expressed as 9 Claude Code build sessions in `Build-Instructions.md` §E, mapped session-by-session onto these weeks.

---

## 7. Team Roles (suggested split, adjust as preferred)

- **V Rohith Pranov:** capacity controller, cost-governance layer, AWS service mapping, and integration/orchestration of the end-to-end run.
- **Sudir Niranj R:** trace generator, classifier, forecaster, and the evaluation/metrics pipeline (plots, tables, statistical comparison).
- **Both:** literature review already complete (report Section 3); both should review the final evaluation together before writeup, since the comparison against the archetype-agnostic baseline is the load-bearing result of the whole project.

---

## 8. Success Metrics (map directly to the report's Section 2.2 outputs)

- **Primary:** statistically significant reduction in mean response time and/or budget-violation magnitude for the archetype-aware policy vs. the archetype-agnostic baseline, on the same trace.
- **Secondary:** positive lead time (minutes) by which the archetype-aware cost-governance layer detects a projected budget breach ahead of the aggregate-only baseline.
- **Tertiary (if stretch attempted):** comparable qualitative behavior between the simplified controller and STAR's full GAT+Transformer policy, to justify the simplification as a faithful-enough substitute for a course timeline.

---

## 9. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Full STAR RL policy (GAT + Transformer + ESRL) is too heavy to train in the time available. | Default to a simpler heuristic or lightweight-RL controller for the Core Prototype; keep the full STAR policy strictly as a Stretch Goal, clearly labeled as such in the report so faculty see the simplification was a deliberate, justified scoping decision, not an oversight. |
| No access to real multi-tenant AWS traffic or the original NASA/Wiki/Ali traces used by STAR. | Use a synthetic trace generator calibrated to resemble published statistics from those traces (burstiness, diurnal pattern) rather than the raw data; document this substitution explicitly in the evaluation section. |
| AWS costs exceed the self-imposed budget ceiling. | Run the Core Prototype's evaluation entirely in simulation (no live AWS spend required for the primary result); reserve real AWS deployment for the Stretch Goal only, with a hard budget alert configured before starting it (see `Build-Instructions.md` T9.1). |
| Comparison against baseline is not apples-to-apples (e.g., archetype-aware policy gets information the baseline doesn't, which is expected, but must be reported honestly). | State explicitly in the evaluation write-up that the comparison isolates the value of archetype information by construction — that is the entire point of the experiment, not a flaw to hide. |

---

## 10. Deliverables Checklist

1. Trace generator script + generated dataset(s), version-controlled.
2. Classifier implementation + accuracy report.
3. Forecaster implementation + per-archetype error metrics.
4. Archetype-aware capacity controller + archetype-agnostic baseline, both runnable on the same trace.
5. Cost-governance layer + budget-breach lead-time comparison.
6. Feedback/recalibration loop + before/after accuracy comparison.
7. Final evaluation report (extends the existing Introduction/Problem Definition/Literature Review/Architecture report already written) with an added Implementation and Results section.
8. (Stretch only) Live AWS deployment notes and/or full STAR-policy training log.

---

## 11. Document Map

- **This file (`PRD.md`)** — what to build, why, and the project timeline.
- **`TRD.md`** — exact data models, function signatures, repo structure, pinned stack, and per-module acceptance gates.
- **`Build-Instructions.md`** — the Claude Code operating contract and task-by-task build prompts that implement this PRD against the TRD.
- **`AWS-Cloud-Architecture-Patent-Disclosure.docx`** — the companion patent disclosure this prototype provides reduction-to-practice evidence for.
- **`Implementation-Guide-VSCode.md`** — the original narrative walkthrough this PRD/TRD/Build-Instructions set formalizes into task form.
