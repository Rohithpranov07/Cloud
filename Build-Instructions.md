# Semantic Autoscaling Prototype — Complete Build Instructions for Claude Code
### Drift-proof, hallucination-resistant prompts aligned 1:1 to the TRD

> **Purpose.** This document builds the Core Prototype *exactly* as specified in `TRD.md`. Every task cites the TRD section it implements and copies its **data model verbatim** as the contract. Follow the tasks in order. Do not skip **§A Operating Contract** or **§B Canonical Specifications** — they are what keep Claude Code from hallucinating, erroring, or drifting.
>
> **Alignment guarantee.** The data models, module contracts, repo structure, and acceptance gates below are transcribed directly from `TRD.md` §1–§6. If any prompt seems to disagree with the TRD, **the TRD wins** — stop and reconcile.
>
> **Scope reminder.** This builds the Core Prototype only (local Python simulation). Phase 9 (Stretch: live AWS) is separate and optional — do not start it before Phases 0–8 pass their VERIFY blocks.

---

# §A. Operating Contract — paste this into `CLAUDE.md` first

> This is the single most important section. It is the standing instruction set for **every** Claude Code session on this repo. Put it verbatim at the top of `CLAUDE.md`.

```md
# Semantic Autoscaling Prototype — Claude Code Operating Contract (READ EVERY SESSION)

## What this project is
A local Python simulation that tests one claim: conditioning cloud autoscaling
and cost-governance decisions on the SEMANTIC ARCHETYPE of incoming AI
inference requests (not just aggregate request rate) reduces simulated
latency-SLA violations and/or improves cost-budget adherence, versus an
archetype-agnostic baseline modeled on the STAR base paper's formulation.
It is the reduction-to-practice evidence for a course project (BCSE355L)
and a companion patent disclosure. It runs entirely on a laptop — no AWS
account is required until the explicitly separate Stretch phase.

## GROUND TRUTH (never change without explicit human instruction)
- Data models, module contracts, repo structure, and acceptance gates are
  FIXED by `TRD.md`. Do not redesign them.
- The four archetypes are FIXED: short_conversational, long_context_rag,
  agentic_tool_using, batch_offline. Do not rename, add, or remove.
- Boundaries: trace/label/forecast/decide/govern/evaluate are separate
  modules under packages `src/trace_gen`, `src/classifier`,
  `src/forecaster`, `src/controller`, `src/cost_governance`,
  `src/feedback`, `src/evaluation`. A module may only import from modules
  earlier in that list, never sideways or from evaluation backward.

## ANTI-HALLUCINATION RULES (hard rules)
1. Never invent method names or return shapes for numpy, pandas,
   scikit-learn, or statsmodels. If unsure of an API's exact signature
   (e.g. `SimpleExpSmoothing`, `DecisionTreeClassifier.predict_proba`),
   check it in this session (docs or `help()`) before using it — do not guess.
2. Never assume a package or version exists. Check `requirements.txt`
   before importing something new; if you need a new dependency, propose
   it explicitly and pin its version before using it.
3. If a data model, function signature, or constant already exists in
   `TRD.md` §1–§2, IMPORT/REUSE it. Never create a second definition of
   the same concept (e.g. a second `ARCHETYPES` list, a second cost table).
4. If a requirement is ambiguous or underspecified, ask ONE clarifying
   question instead of assuming. State any unavoidable assumption
   explicitly in the commit message or PR description.
5. Do not fabricate test results, file contents, or command output. Run
   things and paste the real output.

## ANTI-DRIFT RULES (hard rules)
6. Only create/modify the files listed in the task's "Files you may touch".
   If you must touch another file, list it and explain BEFORE editing.
7. Do not refactor unrelated code. Do not add features not in the task.
   Do not "improve" the architecture beyond the task's scope.
8. Keep every data model byte-aligned with TRD §1. Field names must match
   exactly (e.g. `predicted_archetype`, not `predictedArchetype` or
   `archetype_pred`).

## QUALITY GATES (must hold at end of every task)
- Full type hints on every function in `src/`; `mypy src/` clean (or an
  inline comment explaining a specific, narrow exception).
- No bare `except:` blocks anywhere. Errors are raised or explicitly and
  visibly handled, never silently swallowed.
- Every stochastic function accepts and respects a `seed` parameter
  (NFR-2 in the TRD) — two runs with the same seed must be identical.
- New behaviour ships with a test in `tests/`.
- `pytest` passes clean for the whole `tests/` directory, not just the
  new test.

## WORKING METHOD (every task)
A. First output a SHORT PLAN: the files you'll create/modify and the
   approach. For any task touching >1 file, WAIT for "go" before writing
   (unless told to run autonomously).
B. Implement only what the task asks.
C. Run the task's VERIFY commands. Paste real output. If anything fails,
   fix it before claiming done. Do NOT mark done on unverified work.
D. Extend tests. New behaviour ships with a test.
E. Commit once, message: `feat(<TASKID>): <summary>` (or fix/chore as apt).

## DEFINITION OF DONE
A task is done only when: the code runs (no import/runtime errors), the
task's VERIFY block passes with pasted real output, `pytest` passes
repo-wide, `mypy src/` is clean (or has a documented narrow exception),
the data model matches TRD §1 exactly, and only the permitted files
changed.
```

---

# §B. Canonical Specifications (imported from `TRD.md` — do not paraphrase)

> Every task below references these by name. Full detail lives in `TRD.md`; this is the quick-reference index so tasks don't need to restate it every time.

- **§B.1 Archetypes:** `TRD.md` §1.1 — `ARCHETYPES` list, fixed at four values.
- **§B.2 Data models:** `TRD.md` §1.2–§1.8 — `Request` columns, `TraceConfig`, `ForecastVector`, `ScalingDecision`, `PRIMITIVE_MAP`/unit tables, `BudgetCheckResult`, `EvaluationRow`.
- **§B.3 Module contracts:** `TRD.md` §2 — exact function signatures, one per file.
- **§B.4 Repo structure:** `TRD.md` §3 — fixed directory layout.
- **§B.5 Pinned stack:** `TRD.md` §4 — Python + library versions, exact-pinned in `requirements.txt`.
- **§B.6 Acceptance gates:** `TRD.md` §6 — the pass/fail bar for each module; every task's VERIFY block should satisfy the corresponding row.

---

# §C. How these prompts prevent the three failure modes

| Failure mode | The mechanism in this doc that prevents it |
|---|---|
| **Hallucination** | Anti-hallucination rules 1–5 force verification of every library call and forbid inventing shapes; TRD §1–§2 fix all data models and signatures so nothing is improvised. |
| **Errors** | Every task ends with a **VERIFY** block of exact commands + expected output; Definition of Done blocks progress on unverified work; tests ship with behaviour. |
| **Drift** | Each task lists **exact files you may touch**; data models are copied verbatim from the TRD; module import direction is fixed in §A; "TRD wins" tie-breaker. |

---

# §D. The Build — task-by-task prompts

> **Task anatomy.** Each task has a header (ID · TRD ref · priority · depends-on), then a single **PROMPT** block to paste into Claude Code. Priorities: **[P0]** required for "done" · **[P1]** strengthens the evaluation · **[P2]** stretch/polish. Do all **[P0]** tasks in order first.

---

## PHASE 0 — Foundation

### T0.0 · Operating Contract & context — `CLAUDE.md` · [P0] · depends: none

> **PROMPT**
> Create `CLAUDE.md` at the repo root. Paste into it, verbatim, the "Semantic Autoscaling Prototype — Claude Code Operating Contract" block from §A of the build instructions (I will provide it). Then append a short "Project map" section listing the repo structure from TRD §3 and the fixed `ARCHETYPES` list from TRD §1.1. Do not add anything beyond what §A and the TRD specify.
> **VERIFY:** print `CLAUDE.md`; confirm it contains the GROUND TRUTH, ANTI-HALLUCINATION, ANTI-DRIFT, QUALITY GATES, WORKING METHOD, and DEFINITION OF DONE headings.

---

### T0.1 · Repo scaffold — repo root · [P0] · depends: T0.0

> **PROMPT**
> Goal: a runnable Python project scaffold exactly matching TRD §3.
> Files you may touch: repo root config files; create empty package `__init__.py` files for every directory under `src/` and `tests/`.
> Requirements:
> 1. Create `requirements.txt` with the TRD §4 packages, confirming and pinning exact current-compatible versions (`==`).
> 2. `python -m venv .venv` instructions in `README.md` (stub for now, filled in fully at T8.2).
> 3. `.gitignore`: `.venv/`, `__pycache__/`, `data/*.csv`, `data/*.png`, `.ipynb_checkpoints/`, keep `data/.gitkeep`.
> 4. `configs/default.yaml` holding the tunable constants from TRD §1.3 and §1.6 (trace duration, base rate, shift timing, `PRIMITIVE_UNIT_CAPACITY`, `UNIT_COST_PER_MIN`) so later tasks read from config rather than hardcoding, while keeping the KEYS fixed per TRD §1.6.
> 5. Empty `notebooks/exploration.ipynb`.
> Do NOT implement any module logic yet — scaffold only.
> **VERIFY:** `pip install -r requirements.txt` succeeds; `python -c "import yaml; print(yaml.safe_load(open('configs/default.yaml')))"` prints the config; `pytest` runs with zero tests collected (no errors).

---

## PHASE 1 — Trace Generator (TRD §1.2, §1.3, §2)

### T1.1 · Trace generator core — `src/trace_gen/generator.py` · [P0] · depends: T0.1

> **PROMPT**
> Goal: implement `generate_trace(cfg: TraceConfig = None) -> pd.DataFrame` producing the `Request` schema from TRD §1.2, honoring the flat-aggregate-rate invariant from TRD §1.3.
> Files you may touch: `src/trace_gen/generator.py`, `tests/test_trace_gen.py`.
> Requirements:
> 1. `TraceConfig` dataclass exactly as TRD §1.3 (field names, types, defaults).
> 2. Per-minute Poisson-distributed request count at `base_rate_per_min`, constant across the whole duration — the shift changes only which archetype each request is drawn from, never the count.
> 3. Archetype mix linearly interpolates from `mix_before` to `mix_after` over `[shift_start_min, shift_start_min + shift_duration_min)`.
> 4. `compute_cost` per request: draw from a per-archetype base value with ~15% Gaussian noise, base values reasonable and documented in a comment (e.g. short_conversational cheapest, agentic_tool_using most expensive).
> 5. `tenant_id` uniform over 5 simulated tenants.
> 6. Fully seeded via `np.random.default_rng(cfg.seed)` — no global numpy random state.
> 7. `if __name__ == "__main__":` block writes `data/trace.csv` and prints a quick per-minute count sanity check.
> Do NOT add classifier/forecaster logic here — this module only generates ground-truth data.
> **VERIFY:** `python -m src.trace_gen.generator` writes `data/trace.csv`; `pytest tests/test_trace_gen.py -v` passes, including a test asserting `std/mean < 0.3` on the per-minute aggregate count (TRD §6 acceptance gate) and a determinism test (same seed → identical DataFrame via `pd.testing.assert_frame_equal`).

---

### T1.2 · Trace validation plot — `notebooks/exploration.ipynb` · [P1] · depends: T1.1

> **PROMPT**
> Goal: visually confirm the TRD §1.3 invariant before building anything downstream.
> Files you may touch: `notebooks/exploration.ipynb` only.
> Requirements: load `data/trace.csv`; plot (a) aggregate requests/minute over time, (b) per-archetype share of requests/minute over time, both with a vertical line at `shift_start_min`. Save both figures to `data/` for the record.
> **VERIFY:** paste/describe both plots — (a) should look flat/noisy-flat throughout; (b) should show a visible ramp starting at the shift line for `agentic_tool_using` rising and `short_conversational` falling. If (a) shows a visible bump at the shift, STOP and fix T1.1 before continuing to Phase 2.

---

## PHASE 2 — Classifier (TRD §1.2, §2)

### T2.1 · Proxy features + classifier — `src/classifier/archetype_classifier.py` · [P0] · depends: T1.1

> **PROMPT**
> Goal: implement `add_proxy_features`, `train_classifier`, `classify` exactly per TRD §2's signatures, meeting the `>0.90` accuracy acceptance gate (TRD §6).
> Files you may touch: `src/classifier/archetype_classifier.py`, `tests/test_classifier.py`.
> Requirements:
> 1. `add_proxy_features(df, seed=0) -> pd.DataFrame`: derive `declared_output_len`, `has_tool_schema`, `prompt_domain_score` from `true_archetype` with injected noise (confirm exact noise mechanism matches or reasonably extends the pattern already in TRD §1.2's proxy-feature list) — the classifier must never read `true_archetype` directly downstream.
> 2. `train_classifier(df) -> DecisionTreeClassifier`: `sklearn.model_selection.train_test_split` (confirm exact import path and signature before using it), fit on the three proxy features, print held-out accuracy.
> 3. `classify(df, clf) -> pd.DataFrame`: adds `predicted_archetype` and `confidence` columns per TRD §1.2.
> 4. `if __name__ == "__main__":` reads `data/trace.csv`, runs the pipeline, writes `data/trace_labeled.csv`.
> Do NOT let `classify` or `train_classifier` access `true_archetype` as a feature — only as the training label.
> **VERIFY:** `python -m src.classifier.archetype_classifier` prints accuracy `> 0.90` and writes `data/trace_labeled.csv`; `pytest tests/test_classifier.py -v` passes, including a test that fails loudly if accuracy drops below 0.90 on a fixed seed.

---

## PHASE 3 — Forecaster (TRD §1.4, §2)

### T3.1 · Per-archetype and aggregate forecasters — `src/forecaster/per_archetype_forecaster.py` · [P0] · depends: T2.1

> **PROMPT**
> Goal: implement `build_minute_series`, `forecast_series`, `forecast_all_archetypes`, `forecast_aggregate` exactly per TRD §2, meeting the divergence acceptance gate in TRD §6.
> Files you may touch: `src/forecaster/per_archetype_forecaster.py`, `tests/test_forecaster.py`.
> Requirements:
> 1. Before using `statsmodels.tsa.holtwinters.SimpleExpSmoothing`, confirm its exact constructor and `.fit()`/`.forecast()` signature in this session — do not guess parameter names.
> 2. `build_minute_series(df, archetype_col)`: pivot to a minute-indexed DataFrame, reindexed over the full `0..max(minute)` range with `fill_value=0` so gaps don't break the smoother.
> 3. `forecast_series`: handle the all-zero edge case explicitly (return zeros, don't let the smoother error on a constant-zero series).
> 4. `forecast_all_archetypes` / `forecast_aggregate`: thin wrappers per TRD §2 signatures, using `predicted_archetype` (not `true_archetype`) as the grouping column for the per-archetype path.
> **VERIFY:** `python -m src.forecaster.per_archetype_forecaster` prints both forecast dicts; `pytest tests/test_forecaster.py -v` passes, including a test that runs the forecaster on `data/trace_labeled.csv` windowed at minute 58 and asserts the `agentic_tool_using` forecast's mean exceeds its own trailing 10-minute average (rising trend) while the aggregate forecast's mean does NOT exceed its trailing average by more than a small tolerance (TRD §6 "Forecaster" row).

---

## PHASE 4 — Capacity Controllers (TRD §1.5, §1.6, §2)

### T4.1 · Baseline (archetype-agnostic) controller — `src/controller/baseline_policy.py` · [P0] · depends: T3.1

> **PROMPT**
> Goal: implement `baseline_scaling_decision` exactly per TRD §2's signature and §1.5's `ScalingDecision` shape.
> Files you may touch: `src/controller/baseline_policy.py`, `tests/test_controller.py` (baseline tests only in this task).
> Requirements: single homogeneous pool, `required_units = max(1, round(predicted_load / per_unit_capacity))`, `delta = required_units - current_capacity`, return shape matching TRD §1.5 exactly (`action`, `target_pool="homogeneous_pool"`, `delta`, `new_capacity`).
> **VERIFY:** `pytest tests/test_controller.py -v -k baseline` passes, including a test that `new_capacity >= 1` always holds even for a zero forecast.

---

### T4.2 · Archetype-aware controller — `src/controller/archetype_aware_policy.py` · [P0] · depends: T3.1

> **PROMPT**
> Goal: implement `archetype_aware_scaling_decision` exactly per TRD §2's signature, using the fixed `PRIMITIVE_MAP` and `PRIMITIVE_UNIT_CAPACITY` from TRD §1.6 (import from `configs/default.yaml` via a small loader, not hardcoded — see T0.1).
> Files you may touch: `src/controller/archetype_aware_policy.py`, `tests/test_controller.py` (aware tests only in this task).
> Requirements: aggregate per-archetype forecasts into per-primitive demand via `PRIMITIVE_MAP`; compute `required_units` per primitive using `PRIMITIVE_UNIT_CAPACITY`; return a list of `ScalingDecision` dicts, one per primitive whose `delta != 0` — do not emit a decision for an unchanged primitive.
> **VERIFY:** `pytest tests/test_controller.py -v -k aware` passes, including the TRD §6 acceptance gate test: run on a trace windowed to just after `shift_start_min`, assert at least one returned decision has `target_pool == "eks_gpu_reserved"` and `delta > 0`.

---

### T4.3 · Baseline vs. aware — first head-to-head smoke test — `tests/test_controller.py` · [P1] · depends: T4.1, T4.2

> **PROMPT**
> Goal: an early, minimal end-to-end smoke test (before the full evaluation pipeline in Phase 7) proving the two controllers behave differently around the shift.
> Files you may touch: `tests/test_controller.py` only.
> Requirements: load `data/trace_labeled.csv`, run both controllers minute-by-minute from minute 50 to minute 80 with independent capacity state, assert the two resulting capacity trajectories diverge (e.g. max absolute difference between normalized trajectories exceeds a small threshold) after `shift_start_min`.
> **VERIFY:** `pytest tests/test_controller.py -v -k divergence` passes; paste the two trajectories.

---

## PHASE 5 — Cost Governance (TRD §1.6, §1.7, §2)

### T5.1 · Budget projector — `src/cost_governance/budget_projector.py` · [P0] · depends: T4.2

> **PROMPT**
> Goal: implement `project_spend` and `check_budget` exactly per TRD §2 and the `BudgetCheckResult` shape in TRD §1.7.
> Files you may touch: `src/cost_governance/budget_projector.py`, `tests/test_cost_governance.py`.
> Requirements: `project_spend` sums `units * UNIT_COST_PER_MIN[primitive] * minutes_remaining` over the given capacity dict, using the TRD §1.6 cost table (via config loader). `check_budget`: `breach_projected = projected_spend > budget`; `action = "throttle"` if `overage/budget > 0.2` else `"downgrade_tier"` if breach else `"none"`.
> **VERIFY:** `pytest tests/test_cost_governance.py -v` passes a table-driven test covering: no breach, small breach (downgrade), large breach (throttle) — matching TRD §6's "Cost Governance" acceptance gate.

---

## PHASE 6 — Feedback Loop (TRD §2)

### T6.1 · Recalibration — `src/feedback/recalibration.py` · [P1] · depends: T2.1

> **PROMPT**
> Goal: implement `recalibrate(all_labeled_so_far_df)` per TRD §2, re-fitting the classifier on an expanding window and reusing `train_classifier` from Phase 2 rather than reimplementing training logic.
> Files you may touch: `src/feedback/recalibration.py`, `tests/test_feedback.py`.
> Requirements: import `train_classifier` from `src.classifier.archetype_classifier` — do not duplicate it. Function returns the re-fit classifier; caller is responsible for logging before/after accuracy.
> **VERIFY:** `pytest tests/test_feedback.py -v` passes, including a test that calls `recalibrate` on two nested windows (first 60 minutes, then first 90 minutes) and prints both accuracies, meeting the TRD §6 "Feedback Loop" gate on at least 2 of 3 seeded re-fits.

---

## PHASE 7 — Evaluation (TRD §1.8, §2, §6)

### T7.1 · End-to-end comparison script — `src/evaluation/run_comparison.py` · [P0] · depends: T4.1, T4.2, T5.1, T6.1

> **PROMPT**
> Goal: implement `run()` per TRD §2, producing `data/evaluation_results.csv` (exact `EvaluationRow` columns from TRD §1.8) and `data/capacity_comparison.png`.
> Files you may touch: `src/evaluation/run_comparison.py`, `tests/test_evaluation_end_to_end.py`.
> Requirements:
> 1. Generate the trace, add proxy features, train the classifier, classify — reusing Phase 1–2 functions, not reimplementing.
> 2. Minute-by-minute loop from minute 10 to `duration_minutes - 1`: at each minute, forecast (Phase 3) on the window seen so far, get both controllers' decisions (Phase 4), update their independent capacity state, project both policies' spend (Phase 5).
> 3. Call `recalibrate` (Phase 6) every N minutes (config-driven, e.g. every 20) and log accuracy before/after to stdout.
> 4. Write `data/evaluation_results.csv` with exactly the five TRD §1.8 columns (additional columns allowed, not required ones removed).
> 5. Produce `data/capacity_comparison.png`: baseline vs. aware capacity over time, vertical line at `shift_start_min`.
> Do NOT reimplement any Phase 1–6 logic inline — import and call it.
> **VERIFY:** `python -m src.evaluation.run_comparison` completes without error, prints recalibration accuracy log lines, and writes both output files; `pytest tests/test_evaluation_end_to_end.py -v` passes, asserting the CSV has all five required columns and that `aware_capacity_total` diverges from `baseline_capacity` (in normalized terms) after `shift_start_min` by more than a small threshold — this is the TRD §6 "Evaluation" gate and the project's headline result.

---

## PHASE 8 — Hardening, Tests, Documentation

### T8.1 · Full test suite + mypy pass — repo-wide · [P0] · depends: T7.1

> **PROMPT**
> Goal: close any gaps against TRD §5 (Non-Functional Requirements) and §6 (Acceptance Criteria) across the whole repo.
> Files you may touch: any file under `tests/`; type-hint fixes anywhere under `src/` (no logic changes without flagging them first).
> Requirements: add `mypy` to `requirements.txt` if not already present; run `mypy src/` and fix or explicitly comment-justify every reported issue; confirm every function in `src/` has full type hints (NFR-4); grep for `except:` with no exception type and fix any found (NFR-3); confirm every stochastic function's determinism (NFR-2) has a corresponding test.
> Do NOT change any function's external behavior — type-hint and test additions only.
> **VERIFY:** `mypy src/` clean (or paste the documented exceptions); `pytest -v` passes repo-wide; paste a `grep -rn "except:" src/` showing zero bare excepts.

---

### T8.2 · README + reproducibility — `README.md` · [P0] · depends: T8.1

> **PROMPT**
> Goal: a from-scratch-clone-to-results README, matching what the HELIX build's T9.2 does for its project.
> Files you may touch: `README.md` only.
> Requirements: project summary (2–3 sentences, the core claim from TRD §0); setup steps (`venv`, `pip install -r requirements.txt`); how to run each phase individually and the full `run_comparison.py`; where outputs land (`data/evaluation_results.csv`, `data/capacity_comparison.png`); a short "How this maps to the report" section linking each `src/` module to the corresponding subsection of the course report's Section 4; a "Results" section with the actual numbers/plot produced by the last verified run of `run_comparison.py`.
> Do NOT introduce new features here — documentation only.
> **VERIFY:** from a genuinely fresh clone (or a clean temp directory copy), follow the README verbatim end-to-end and confirm it reproduces `data/evaluation_results.csv` and the plot with no undocumented steps.

---

## PHASE 9 — Stretch (optional, separate from "done")

### T9.1 · AWS Budgets safety net — AWS Console · [P2] · depends: T8.2

> **PROMPT**
> Goal: create a hard cost ceiling BEFORE any AWS resource in this phase is created.
> Files you may touch: none (console/CLI action, document the steps taken in `docs/stretch_aws_notes.md`).
> Requirements: create an AWS Budgets budget (e.g. $10) with an email alert at 80% and 100%; record the budget name/threshold in `docs/stretch_aws_notes.md`.
> **VERIFY:** screenshot or CLI output confirming the budget exists, saved into `docs/stretch_aws_notes.md`.

---

### T9.2 · Classifier as Lambda + API Gateway — `stretch/` · [P2] · depends: T9.1

> **PROMPT**
> Goal: deploy `classify()`'s logic behind a real HTTP endpoint, as a stretch demonstration only — this does NOT replace or modify anything in `src/`.
> Files you may touch: new `stretch/` directory only; do not modify `src/`.
> CRITICAL anti-hallucination step: before writing any AWS SDK/CDK/SAM code, open the current Lambda + API Gateway docs in this session and confirm exact handler signatures, event shapes, and deployment commands for whichever tool you choose (`boto3`, AWS SAM, or CDK) — do not invent CLI flags or event JSON shapes.
> Requirements: a minimal Lambda handler wrapping the already-tested `classify` logic; API Gateway HTTP API in front of it; document teardown commands in `docs/stretch_aws_notes.md`.
> **VERIFY:** a real `curl`/Postman request to the deployed endpoint returns a `predicted_archetype`; paste the request/response; then paste the teardown command output confirming resources were deleted.

---

# §E. Build Order

| Session | Tasks | Outcome |
|---|---|---|
| 1 | T0.0, T0.1 | Contract, scaffold, config, pinned deps. |
| 2 | T1.1, T1.2 | Trace generator validated — flat aggregate, visible archetype shift. |
| 3 | T2.1 | Classifier hits >90% accuracy. |
| 4 | T3.1 | Per-archetype forecaster shows early divergence from aggregate-only. |
| 5 | T4.1, T4.2, T4.3 | Both controllers implemented; smoke test shows they diverge after the shift. |
| 6 | T5.1, T6.1 | Cost governance + feedback loop implemented and tested. |
| 7 | T7.1 | Full `run_comparison.py` — headline result produced. |
| 8 | T8.1, T8.2 | Hardened, documented, reproducible from a fresh clone. |
| 9 (optional) | T9.1, T9.2 | Live AWS demonstration, torn down after. |

This maps directly onto the PRD's Week 1–10 plan: Sessions 1–2 ≈ Weeks 1–2, Session 3 ≈ Week 3, Sessions 4–6 ≈ Weeks 4–7, Session 7–8 ≈ Week 8, Session 9 ≈ Weeks 9–10 if attempted.

---

# §F. Final Acceptance — "the Core Prototype is completely built" when:

1. **Trace (TRD §1.3):** `generate_trace` produces a trace where aggregate rate stays flat (`std/mean < 0.3`) while archetype composition visibly shifts. ✅
2. **Classifier (TRD §6):** held-out accuracy `> 0.90`, verified by a passing test. ✅
3. **Forecaster (TRD §6):** per-archetype forecast for the shifting archetype rises measurably earlier than the aggregate-only forecast, verified by a passing test. ✅
4. **Controllers (TRD §6):** archetype-aware controller issues a scaling decision toward `eks_gpu_reserved` within 10 minutes of the shift; baseline and aware capacity trajectories are shown to diverge. ✅
5. **Cost Governance (TRD §6):** `check_budget` correctly classifies no-breach / downgrade / throttle cases, verified by a passing table-driven test. ✅
6. **Feedback (TRD §6):** recalibration improves or holds classifier accuracy across an expanding window, verified by a passing test. ✅
7. **Evaluation (TRD §6):** `run_comparison.py` runs end-to-end from a fresh clone, per the README, and produces `evaluation_results.csv` + `capacity_comparison.png` showing the divergence described in item 4. ✅
8. **Gates:** `pytest -v` passes repo-wide; `mypy src/` is clean (or has documented exceptions); every file touched was on an approved list per its task. ✅

> If all eight hold, the Core Prototype is built exactly as `TRD.md` specifies — no drift between spec and system. Only after this is true should Phase 9 (Stretch) be attempted.

**This prototype exists to answer one question: does knowing what kind of request is coming, not just how many, make autoscaling and cost governance measurably better? Everything above exists to give that question an honest, reproducible answer.**
