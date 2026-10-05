# Semantic Autoscaling — Build Instructions, Part 2 (to 100%)
### Latency evaluation · statistical evidence · live AWS integration — drift-proof prompts aligned 1:1 to `TRD-AWS-Addendum.md`

> **Where this starts.** Phases 0–8 of `Build-Instructions.md` are done and verified: 212 tests pass, `mypy src/` is clean, and the README reproduces the headline. Part 2 continues the numbering at **Phase 10**. Phase 9 of Part 1 is absorbed as follows:
> - **T9.1 (AWS Budgets)** becomes **T11.1**. If you already created the budget, T11.1 is a check, not a rebuild.
> - **T9.2 (classifier-only Lambda)** is superseded by Phase 12. If you deployed it, delete that stack first (T11.0 covers this).
>
> **Spec.** Every task here imports from `TRD-AWS-Addendum.md` (§8–§14). Copy it to the repo root before T10.0. The order of precedence is `TRD.md` §1–§2 (for anything already built) → `TRD-AWS-Addendum.md` → this file.
>
> **Order matters.** Phases 10 and 11.1 need no AWS spend and produce the PRD's primary metric, so do them first. If time runs out after Phase 10, the project still has latency results and a significance test, and the AWS phase is honestly reported as not done. The reverse order would leave live infrastructure with nothing rigorous to compare it against.

---

# §A2. Operating Contract — additions to `CLAUDE.md`

Append this block to the end of the existing `CLAUDE.md` in T10.0, word for word. The original contract stays in force.

```markdown
## PART 2 ADDITIONS (Phases 10–14) — READ EVERY SESSION

### Ground truth, extended
- `TRD-AWS-Addendum.md` §8–§14 is FIXED for Part 2 work exactly as `TRD.md` is for Part 1.
- New package boundary: `cloud/` may import from `src/`; `src/` must NEVER import
  `cloud/`, `boto3`, or anything AWS. `tests/cloud/` tests `cloud/` only.
- Default behaviour of every Part 1 function is frozen. Part 2 may add keyword-only
  parameters whose defaults reproduce Part 1 output byte-for-byte (golden-file test).

### AWS anti-hallucination rules (hard)
A1. Before writing ANY AWS code, template property, CLI command, or event shape, open
    the current AWS documentation page for it IN THIS SESSION and cite the URL in a code
    comment next to its first use. Never invent CLI flags, SAM properties, IAM actions,
    or event JSON. If docs and memory disagree, docs win.
A2. Never run a command that creates, modifies, or deletes AWS resources without first
    printing the exact command and waiting for "go". Read-only `describe/get/list`
    calls are fine.
A3. Never put account IDs, ARNs, emails, or keys in committed files.
A4. `pytest` must pass offline with no AWS credentials set. Use
    `botocore.stub.Stubber` for AWS clients; never call real AWS from a test.

### Cost guardrails (hard)
C1. No `sam deploy` unless `aws budgets describe-budgets` shows the USD 5 budget.
C2. No GPU instances, SageMaker endpoints, EKS clusters, NAT gateways, or provisioned
    capacity of any kind. If a task seems to need one, STOP and ask.
C3. Every working session that deploys ends with teardown (T14.1) unless the human
    explicitly says to keep the stack up, and the decision is logged in
    `docs/aws_live_notes.md`.

### Honesty rules for results (hard)
H1. Never tune a parameter after seeing a latency or significance result. Parameters
    are fixed in config BEFORE the run; changing one afterwards needs a logged reason
    and a re-run of everything that depends on it.
H2. Report both sizing rules (round, ceil) for every latency claim (Addendum §8.1).
H3. No test outcome or p-value is an acceptance gate. Gates check correctness;
    results are reported as measured.
```

---

# §B2. Canonical specifications (index into the Addendum)

- **§B2.1 Physical model:** Addendum §9.1 covers slots per unit, derived service times, arrivals, FIFO queues, initial capacity and SLAs.
- **§B2.2 Config keys:** Addendum §9.2.
- **§B2.3 Contracts:** Addendum §9.3 (sizing keyword), §9.4 (latency model), §9.5 (refactor), §10.1 (multi-seed) and §11.4 (handlers).
- **§B2.4 Layout:** Addendum §11.2.
- **§B2.5 Data models:** Addendum §11.3 covers the ingress body, the SQS message and the three DynamoDB tables.
- **§B2.6 Infrastructure:** Addendum §11.5.
- **§B2.7 NFRs and gates:** Addendum §12 and §13.

---

# §D2. The build — task-by-task prompts

> The task anatomy and priority labels are the same as in Part 1. **[P0]** tasks are needed for 100%, **[P1]** tasks strengthen the evaluation, and **[P2]** tasks are optional polish. Paste each PROMPT block into Claude Code in VS Code with the repo open.

---

## PHASE 10 — Close the evaluation gaps in simulation (no AWS, ₹0)

### T10.0 · Part 2 setup — docs, contract, golden file · [P0] · depends: Part 1 done

> **PROMPT**
> Goal: install the Part 2 spec and freeze a golden reference before any code changes.
> Files you may touch: `TRD-AWS-Addendum.md` (new, I will paste it), `CLAUDE.md` (append only), `TRD.md` (one line), `README.md` (two corrections only), `tests/golden/evaluation_results_round.csv` (new), `.gitignore`.
> Requirements:
> 1. Save the Addendum I paste as `TRD-AWS-Addendum.md`. At the top of `TRD.md`, under the scope note, add exactly one line: `> Part 2 (latency, statistics, live AWS) is specified in TRD-AWS-Addendum.md §8–§14.`
> 2. Append the §A2 block to `CLAUDE.md` exactly as given.
> 3. Run `python -m src.evaluation.run_comparison` and copy the resulting `data/evaluation_results.csv` to `tests/golden/evaluation_results_round.csv`. Un-ignore that one path in `.gitignore`.
> 4. Fix two README statements so they match the verified run. **Read the actual values from the fresh CSV; do not take mine on trust.**
>    - The controller scale-up minute: the README says +12. The first minute with `aware_capacity_eks_gpu_reserved >= 2` in the CSV was 71 (+11) in my verification run.
>    - The aggregate-forecast deviation: the README says "within ±1%". My verification run measured a maximum of 1.3% after the shift. Recompute it with the same method as `test_aggregate_forecast_stays_blind_to_the_shift`.
>
>    Also explain in one sentence why the comment in `archetype_aware_policy.py` says 72: it was measured before recalibration was wired into the evaluation loop. Confirm that this is true from `git log -p`; if it isn't, say what is.
> 5. If a Part 1 T9.2 stack is still deployed, print the teardown command and wait for "go". If a `stretch/` directory exists, list its contents and ask before deleting it.
> **VERIFY:** `git diff --stat` shows only the permitted files; paste the two README diffs and the CSV values that justify them; `pytest -q` still passes; `head -3 tests/golden/evaluation_results_round.csv`.

---

### T10.1 · Sizing rule as a keyword (round default, ceil option) · [P0] · depends: T10.0

> **PROMPT**
> Goal: implement Addendum §9.3, so both controllers accept `sizing: str = "round"`. Default behaviour must be byte-identical.
> Files you may touch: `src/controller/baseline_policy.py`, `src/controller/archetype_aware_policy.py`, `src/config.py` (validation only), `configs/default.yaml` (add `evaluation.sizing_rule: round`), `src/evaluation/run_comparison.py` (thread the config value through, nothing else), `tests/test_controller.py`, `tests/test_sizing_golden.py` (new).
> Requirements:
> 1. Put one private helper per module, `_required_units(demand, unit_capacity, sizing)`. Its `round` branch must be exactly the current expression. Its `ceil` branch is `max(MIN_CAPACITY, math.ceil(demand / unit_capacity))`. The aware policy keeps its zero-demand → 0 rule under both. Any other `sizing` value raises `ValueError`.
> 2. Update the long rounding comment in `archetype_aware_policy.py` so it records that the human decision has been made: keep `round` as the default and report `ceil` alongside it (Addendum §8.1). Do not delete the reasoning.
> 3. In `test_sizing_golden.py`, run the evaluation with the default config and assert that the produced CSV is equal to `tests/golden/evaluation_results_round.csv` (`pd.testing.assert_frame_equal`, exact).
> 4. Add table-driven tests for `ceil` in both controllers. Use boundary cases 0, exact multiples, multiple + 0.01, and multiple + 0.5, and run them under both rules.
> **VERIFY:** `pytest -q` passes, including the golden test; `mypy src/` is clean; paste the new parametrised test IDs; paste `python -m src.evaluation.run_comparison | tail -20` showing the unchanged headline.

---

### T10.2 · Request-level latency model · [P0] · depends: T10.1

> **PROMPT**
> Goal: implement `src/evaluation/latency_model.py` exactly per Addendum §9.1, §9.2 and §9.4.
> Files you may touch: `src/evaluation/latency_model.py` (new), `configs/default.yaml` (add the `latency:` block exactly as in §9.2), `src/config.py` (export `LATENCY_DEFAULTS` and validate it), `tests/test_latency_model.py` (new).
> Requirements:
> 1. Derive `BASE_SERVICE_S` at import time from `PRIMITIVE_UNIT_CAPACITY`, `PRIMITIVE_MAP` and `slots_per_unit`. Do not write the numbers 4/8/12/10 anywhere in source; a test asserts them.
> 2. `simulate_pool`: an event-driven FIFO multi-server queue. Keep a heap of slot-free times. A request starts at `max(arrival, earliest time a slot is free AND busy count < allowed(minute(t)))`. When the allowed count drops, nothing is pre-empted. Implement it with an explicit loop over requests in arrival order plus a `heapq` of running end times; no pandas inside the hot loop. Document its complexity in the docstring.
> 3. `simulate_policy_latency` routes by `predicted_archetype` (aware → `PRIMITIVE_MAP`, baseline → `homogeneous_pool`) and charges service by `true_archetype` (§9.1). Its output columns are exactly those listed in §9.4.
> 4. `summarise_latency` returns plain floats and ints only, so the result is JSON-serialisable. Windows: pre = `minute < shift_start`; during = `[shift_start, shift_end)`; post = `minute >= shift_end`.
> 5. Write tests for every row of the Addendum §13 "Latency model" gate, (a) through (e). For (d), the M/M/1 check, the test generates its own exponential arrivals and services with a seeded rng and calls `simulate_pool` with a constant 1 slot. For (b), reconstruct the busy count at every start and end event and assert that it never exceeds the allowed count.
> Do NOT call this module from `run_comparison` yet; T10.3 does that.
> **VERIFY:** `pytest tests/test_latency_model.py -v` (paste); `mypy src/`; paste the M/M/1 measured vs analytic numbers; paste `python -c "from src.evaluation.latency_model import BASE_SERVICE_S; print(BASE_SERVICE_S)"`.

---

### T10.3 · Wire latency into the evaluation (both sizing rules) · [P0] · depends: T10.2

> **PROMPT**
> Goal: Addendum §9.5. Extract `simulate()`, add per-policy realised spend, and produce the latency outputs for both sizing rules.
> Files you may touch: `src/evaluation/run_comparison.py`, `tests/test_evaluation_end_to_end.py`, `tests/test_sizing_golden.py` (only if the refactor needs the golden comparison pointed at `simulate()`), `README.md` (new "Latency" subsection).
> Requirements:
> 1. Move the minute loop out of `run()` into `simulate(cfg, sizing="round") -> tuple[results_df, labeled_df]`, with no file I/O and no prints except the existing recalibration lines. Then `run()` = simulate(round) + simulate(ceil) + the outputs. The golden test must still pass for `round`.
> 2. Add a `baseline_actual_spend` column (cumulative baseline units × `_blended_unit_cost`). Keep `cumulative_actual_spend` as it is; it is the aware policy's actual spend.
> 3. For each sizing rule and each policy, call `simulate_policy_latency` and write `data/latency_per_request_{policy}_{sizing}.csv`. Write `data/latency_summary.json` with keys `{sizing: {policy: summarise_latency(...)}}`.
> 4. Write `data/latency_comparison.png`, using the colours already used by `_plot` and two panels:
>    - (a) post-shift p95 response per archetype, grouped by policy, one subplot column per sizing rule;
>    - (b) the rolling 5-minute SLA-violation rate over time for both policies, with the shift window shaded.
>
>    Give it a legend and no dual axis.
> 5. Extend `_summarise` to print a latency table: policy × sizing × window, with p95 response and SLA-violation rate.
> 6. README: add a "Latency" subsection that pastes the actual printed table and explains the Addendum §8.1 finding in 3–4 sentences. It must state plainly that under `round` the baseline is under-provisioned before the shift.
> **VERIFY:** `python -m src.evaluation.run_comparison` (paste the full latency table); `pytest -q`; `mypy src/`; describe what the PNG shows in two sentences. If the aware policy does NOT beat the baseline post-shift under `ceil`, say so plainly. That is a result, not a bug, unless a test proves otherwise.

---

### T10.4 · 30-seed paired evaluation + significance · [P0] · depends: T10.3

> **PROMPT**
> Goal: implement `src/evaluation/multiseed.py` per Addendum §10.1.
> Files you may touch: `src/evaluation/multiseed.py` (new), `src/evaluation/detection.py` (new: the shared detection helper), `tests/test_forecaster.py` (switch only the detection test to the shared helper; the assertions stay the same), `tests/test_multiseed.py` (new), `configs/default.yaml` (add the `multiseed:` block), `src/config.py` (validation), `README.md` ("Statistical evaluation" subsection).
> Requirements:
> 1. `detection.first_detection_minute(labeled, cfg, threshold=0.25, horizon=5) -> int | None`: the exact logic currently inside `test_per_archetype_detects_the_shift_before_the_aggregate_does`, moved once. The test then calls it.
> 2. `run_seed(seed, sizing)` builds a `TraceConfig` from the defaults with `seed=seed`, keeps the classifier seed at `multiseed.classifier_seed`, calls `simulate()` and the latency model, and returns the §10.1 metric row. A metric that never occurs is NaN.
> 3. `run_all(..., workers=N)` uses `concurrent.futures.ProcessPoolExecutor` when `workers > 1`. Results must be identical to `workers=1`; test this on 2 seeds.
> 4. `paired_tests`: for each sizing rule and each of the three metrics, use `scipy.stats.wilcoxon(baseline, aware, alternative="two-sided")`. Before writing that line, check the signature and the zero-difference handling (`zero_method`) in the installed scipy (1.18.1) with `help()`, per rule 1. Also compute the median paired difference, a seeded bootstrap 95% CI (10,000 resamples), and the matched-pairs rank-biserial r. Drop NaN pairs and report the n actually used.
> 5. `main()` writes `data/multiseed_results.csv`, `data/multiseed_summary.json` and `data/multiseed_distributions.png` (paired dot-and-line plot per metric, one panel per sizing rule).
> 6. README: a results table with metric × sizing → median baseline, median aware, median paired difference [CI], p, r, n. Add one paragraph of interpretation that claims nothing the table does not show.
> **VERIFY:** `time python -m src.evaluation.multiseed --workers 4` (paste the summary JSON); `pytest -q`; `mypy src/`. State the wall time; my estimate is about 7 minutes single-process at 15 s per run.

---

### T10.5 · Sensitivity grid · [P1] · depends: T10.4

> **PROMPT**
> Goal: Addendum §10.2, i.e. how large a composition shift must be before archetype awareness pays off.
> Files you may touch: `src/evaluation/sensitivity.py` (new), `tests/test_sensitivity.py` (new, a 1-cell × 1-seed smoke test only), `README.md`.
> Requirements: reuse `run_seed`. Build the `mix_after` variants by moving share between `short_conversational` and `agentic_tool_using` only, then assert that the mix sums to 1 and the flat-aggregate invariant still holds for every variant before running it. Pass the SLA multiplier through as an override, without editing config on disk. Output `data/sensitivity.csv` and `data/sensitivity_heatmap.png` (post-shift SLA-violation-rate difference, baseline − aware, using a diverging palette with grey at 0 and annotated cells, one heatmap per sizing rule).
> **VERIFY:** paste the heatmap's numbers as a table, and name the smallest agentic share at which the difference is positive in ≥ 8 of 10 seeds under `ceil`, or state that none qualifies.

---

## PHASE 11 — AWS foundation (₹0, do before any deploy)

### T11.1 · Account safety: budget, MFA, CLI profile, quota · [P0] · depends: T10.4

> **PROMPT**
> Goal: Addendum NFR-A1 and NFR-A3, plus a safe identity. This is mostly console work that I do. You give the exact steps and verify read-only.
> Files you may touch: `docs/aws_live_notes.md` (new).
> Requirements:
> 1. Give me the console steps, and check each against the current AWS docs (rule A1) before giving it:
>    - (a) Turn on MFA for the root user.
>    - (b) Create an IAM Identity Center user, or an IAM user if Identity Center is unavailable, with admin access for this project. Never use root keys.
>    - (c) Run `aws configure sso` or `aws configure`, using profile `semauto` and region `ap-south-1`.
>    - (d) Create a monthly cost budget of USD 5 with alerts at 50%, 80% and 100% actual and 100% forecast to my email. If T9.1 already created one, verify it instead.
>    - (e) Turn on the `project` cost-allocation tag. It only starts appearing after resources carrying it exist, so note that.
>    - (f) Check the Free Tier status page, and record whether the account is on the free plan or the paid plan and how much credit remains.
>
>    Then verify with read-only commands:
>    - `aws sts get-caller-identity --profile semauto` (print the account ID *redacted* in the notes);
>    - `aws budgets describe-budgets --account-id <id> --profile semauto`;
>    - `aws service-quotas get-service-quota --service-code lambda --quota-code L-B99A9384 --region ap-south-1 --profile semauto`. Verify that this quota code is "Concurrent executions" in the docs before running it.
> 2. If the applied quota is < 50: give me the console steps to request 100 through Service Quotas, and record the request ID. **Fallback if it is denied or still pending:** run Phase 13 with `base_rate_per_min` halved to 25 for both the simulation and the live run, with everything else unchanged. Log that this fallback was used, and re-run T10.3 at that rate so that live and simulated results stay comparable.
> 3. Record everything in `docs/aws_live_notes.md` under the headings: Identity, Budget, Free-tier status, Concurrency quota, Decisions.
> **VERIFY:** paste the redacted outputs of the three read-only commands and the notes file.

---

### T11.2 · Toolchain: AWS CLI v2, SAM CLI, Docker (arm64) · [P0] · depends: T11.1

> **PROMPT**
> Goal: a pinned, verified local toolchain for building arm64 Lambda images.
> Files you may touch: `docs/aws_live_notes.md` ("Toolchain" section), `requirements-cloud.txt` (new).
> Requirements: give install steps for my OS (macOS, Apple Silicon) from the official docs: AWS CLI v2, AWS SAM CLI and Docker Desktop. Create `requirements-cloud.txt` with `boto3==<latest>`, checking the latest version on PyPI in this session, and install it into `.venv`. Confirm `botocore.stub.Stubber` imports.
> **VERIFY:** paste `aws --version`, `sam --version`, `docker version --format '{{.Server.Arch}}'` (expect `arm64`), `python -c "import boto3, botocore.stub; print(boto3.__version__)"`, and `pytest -q` still green.

---

## PHASE 12 — Build the live control plane

### T12.1 · `cloud/` skeleton, container image, build-time model · [P0] · depends: T11.2

> **PROMPT**
> Goal: one Lambda container image that holds the unchanged `src/` and a classifier trained at build time (Addendum §11.2, NFR-A5).
> Files you may touch: `cloud/__init__.py`, `cloud/Dockerfile`, `cloud/requirements-lambda.txt`, `cloud/build_model.py`, `cloud/common.py`, `.dockerignore`, `tests/cloud/__init__.py`, `tests/cloud/test_build_model.py`.
> Requirements:
> 1. Before writing the Dockerfile, open the AWS docs for "Deploy Python Lambda functions with container images" and the `public.ecr.aws/lambda/python` image page, and confirm that the `3.14` arm64 tag exists. Cite both URLs in a comment.
> 2. `requirements-lambda.txt` contains exactly the pins from `requirements.txt` for numpy, pandas, scipy, scikit-learn, statsmodels and PyYAML, plus the boto3 pin. It has no matplotlib, pytest or mypy. Add a comment saying the pins must match `requirements.txt`, and a test that enforces this.
> 3. The Dockerfile copies `src/`, `configs/` and `cloud/` into `${LAMBDA_TASK_ROOT}`, installs the requirements, then runs `python -m cloud.build_model`. That step calls `generate_trace(_default_config())` → `add_proxy_features(seed=cfg.seed)` → `train_classifier(seed=cfg.seed)`. These are **the same seeds `run_comparison.simulate()` uses (42), not the seed-0 ones in `src.classifier.main`**, so live predictions equal the simulation's pre-recalibration predictions. It saves the model with `joblib` to `model/classifier.joblib`. `CMD` defaults to the router handler.
> 4. `common.py` holds the env-var loading (table names, queue URLs, ESM UUIDs, `SLOTS_PER_UNIT`, `TIME_SCALE`, `ENFORCE_GOVERNANCE`), a `emit_emf(namespace, dimensions, metrics)` helper that prints one EMF JSON line (verify the format against the CloudWatch EMF spec page and cite it), and lazy boto3 client factories.
> 5. Test: `build_model` produces a model whose predictions on the seed-42 trace equal the pre-recalibration `predicted_archetype` from `simulate(recalibrate=False)` (100%). If T12.4 has not added that keyword yet, compare against `classify()` with seed-42 features and the seed-42 model.
> **VERIFY:** `docker build --platform linux/arm64 -t semauto-core -f cloud/Dockerfile .` (paste the tail and the image size from `docker images`); `docker run --rm --entrypoint python semauto-core -c "import src, sklearn, statsmodels; print('ok')"`; `pytest -q`.

---

### T12.2 · Router handler (ingress + classify + count + enqueue) · [P0] · depends: T12.1

> **PROMPT**
> Goal: `cloud/handlers/router.py` per Addendum §11.3–§11.4.
> Files you may touch: `cloud/handlers/__init__.py`, `cloud/handlers/router.py`, `tests/cloud/test_router.py`.
> Requirements:
> 1. Confirm the HTTP API **payload format version 2.0** event and response shape in the API Gateway docs, and cite the URL.
> 2. A pure function `route(body, clf, ddb, sqs, queue_urls, now) -> dict` does the work:
>    - validate the body against §11.3 (return 400 with a message on any missing or mistyped field);
>    - build a one-row DataFrame from the three feature columns only and call `src.classifier.archetype_classifier.classify`;
>    - choose the pool: aware → `PRIMITIVE_MAP[predicted]`, baseline → `homogeneous_pool`;
>    - run `UpdateItem` on `semauto-counts` with `ADD #a :one` (an expression attribute name for the archetype) and `SET expires_at`;
>    - `SendMessage` with the §11.3 message body including `ingress_ts=now()`;
>    - return 202 with the §11.3 response.
> 3. The model loads once per container (module-level lazy load), never per request.
> 4. Emit one EMF line per request: namespace `SemAuto`, dimensions `{run_id, policy, predicted_archetype}`, metric `Requests=1`.
> 5. Tests use `Stubber` on real `boto3.client("dynamodb"/"sqs", region_name="ap-south-1")` objects with expected params. They cover:
>    - a happy path for each policy;
>    - 400 on a bad body;
>    - routing correctness for all 4 archetypes;
>    - **scrambling `true_archetype` changes neither the prediction nor the pool** (the same idea as the forecaster test);
>    - no network: run the tests with `AWS_ACCESS_KEY_ID` unset.
> **VERIFY:** `pytest tests/cloud -v`; local Lambda emulator test: `docker run --rm -p 9000:8080 semauto-core` and then `curl -XPOST localhost:9000/2015-03-31/functions/function/invocations -d @tests/cloud/fixtures/http_v2_event.json`. Expect an AWS error, because there are no credentials or tables locally. Paste it and confirm that the handler got as far as the DynamoDB call, which proves the image imports and classifies.

---

### T12.3 · Worker handler (emulated serving pool) · [P0] · depends: T12.1

> **PROMPT**
> Goal: `cloud/handlers/worker.py`, a stdlib-plus-boto3-only zip function (Addendum §11.1, §11.4).
> Files you may touch: `cloud/handlers/worker.py`, `tests/cloud/test_worker.py`.
> Requirements: confirm the SQS → Lambda event shape and `ReportBatchItemFailures` in the docs (cite them). For each record (batch size is 1 by design, but loop anyway): parse the body; `start_ts=time.time()`; `time.sleep(service_s / TIME_SCALE)`; `end_ts=time.time()`; `PutItem` to `semauto-results` with the §11.3 fields and `expires_at`. On any exception for a record, add its `messageId` to `batchItemFailures`; never swallow it silently, and log it. Import nothing from `src/` (this is a zip function). The sleep is injected for tests.
> **VERIFY:** `pytest tests/cloud/test_worker.py -v`, covering the happy path, a malformed body → batchItemFailures, and that the injected sleep receives `service_s / TIME_SCALE`.

---

### T12.4 · Controller handler (forecast → decide → actuate → govern → record) · [P0] · depends: T12.1

> **PROMPT**
> Goal: `cloud/handlers/controller.py` per Addendum §11.4. It reuses `src/` for all logic.
> Files you may touch: `cloud/handlers/controller.py`, `cloud/adapters.py` (new), `tests/cloud/test_controller.py`, `tests/cloud/test_adapters.py`.
> Requirements:
> 1. `adapters.counts_to_labeled(items) -> pd.DataFrame` expands the per-minute counter items into the minimal request-level frame (`minute`, `predicted_archetype`) that `forecast_all_archetypes` and `forecast_aggregate` already accept, using `np.repeat`. Minutes with no counts become empty, so the forecaster's zero-fill handles them. Test: on the seed-42 labelled trace, counts → frame → `build_minute_series` equals `build_minute_series` on the original frame.
> 2. Tick, for `{"run_id","policy","sizing","minute"}`:
>    - Query counts for `minute ≤ m`, using `ConsistentRead`, which is valid on base-table queries; confirm this in the docs.
>    - Read the previous capacity from the latest decision item, or the §9.1 initial capacity if there is none.
>    - Forecast and decide with the `src` functions and the `sizing` keyword.
>    - Actuate with `update_event_source_mapping(UUID=..., ScalingConfig={"MaximumConcurrency": max(2, SLOTS_PER_UNIT*units)})`, only for pools whose value changed. Confirm the boto3 parameter names in the docs.
>    - Projected spend and `check_budget`: `incurred` is the sum of `project_spend(capacity, 1)` over all previous decisions plus this one, which is the same definition as `run_comparison`. The baseline projection uses `_blended_unit_cost`; import it, never copy it.
>    - `PutItem` the decision (§11.3).
>    - SNS publish on the first breach for the run only. Store a `notified` flag in the decision item to guarantee this.
>    - Emit EMF: per-pool `Units`, per-archetype `ForecastPeak`, `ProjectedSpend` and `Budget`.
> 3. `{"reset": {...}}` sets every ESM to its initial value and returns. `{"warmup": true}` imports everything and returns.
> 4. Tests (with Stubber) cover:
>    - the actuation rule `max(2, 2·units)`;
>    - no API call when nothing changed;
>    - SNS is sent exactly once across a sequence of breaching ticks;
>    - first tick with no previous decision → initial capacity;
>    - **parity**: driving `tick()` minute-by-minute over stubbed counts from the seed-42 trace (no recalibration, which is not deployed) gives the same capacity sequence as `simulate()` with recalibration disabled.
>
>    Add a keyword-only `recalibrate: bool = True` to `simulate()` for that test; the default is unchanged and the golden test still passes.
> **VERIFY:** `pytest -q` (whole repo, offline); `mypy src/`; paste the parity test output.

---

### T12.5 · SAM template · [P0] · depends: T12.2, T12.3, T12.4

> **PROMPT**
> Goal: `cloud/template.yaml` and `cloud/samconfig.toml` exactly per Addendum §11.5. This is the only place infrastructure is defined.
> Files you may touch: `cloud/template.yaml`, `cloud/samconfig.toml`, `tests/cloud/test_template.py`.
> Requirements:
> 1. Open the SAM and CloudFormation docs and cite them in comments for each of these before writing it: `AWS::Serverless::Function` with `PackageType: Image` and `Metadata` (`Dockerfile`, `DockerContext`, `DockerTag`); `AWS::Serverless::HttpApi` and the `HttpApi` event; `AWS::Lambda::EventSourceMapping` (`BatchSize`, `ScalingConfig.MaximumConcurrency`, and `Ref` returning the UUID); `AWS::SQS::Queue` (`VisibilityTimeout`); `AWS::DynamoDB::Table` (`BillingMode: PAY_PER_REQUEST`, `TimeToLiveSpecification`); `AWS::SNS::Topic` with an email `Subscription`; `AWS::CloudWatch::Dashboard`; the `ScheduleV2` event with `State: DISABLED`; and SAM policy templates (`DynamoDBCrudPolicy`, `SQSSendMessagePolicy`, `SNSPublishMessagePolicy`), falling back to explicit IAM statements where no template fits `lambda:UpdateEventSourceMapping`.
> 2. Parameters: `AlertEmail` (no default), `TimeScale` (default 1), `SlotsPerUnit` (default 2), `EnforceGovernance` (default "false").
> 3. Set `Globals.Function.Architectures: [arm64]` and `Globals.Function.Tags.project: semantic-autoscaling`, and put the same tag on the tables, queues and topic.
> 4. Worker timeout = `ceil(max(service_s))+10`. Compute `max(service_s)` from the seed-42 trace in a comment, and set each queue's visibility timeout to ≥ 6× the worker timeout.
> 5. Dashboard widgets: requests per archetype (stacked), forecast peak per archetype, units per pool, projected spend vs budget, per-queue `ApproximateNumberOfMessagesVisible` and `ApproximateAgeOfOldestMessage`, and worker `ConcurrentExecutions`.
> 6. Outputs: `ApiUrl`, `ControllerFunctionName`, the 5 queue URLs and the 3 table names.
> 7. `test_template.py` parses the YAML with a CloudFormation-tag-aware loader, which you write in the test, about 15 lines. It asserts: every function is arm64; every taggable resource carries `project`; no `ReservedConcurrentExecutions` anywhere; every ESM has `BatchSize: 1` and `MaximumConcurrency: 2`; and there is no EC2, SageMaker, EKS or NAT resource type (rule C2).
> **VERIFY:** `sam validate --lint --template cloud/template.yaml` (paste); `sam build --template cloud/template.yaml` (paste the tail); `pytest -q`.

---

### T12.6 · Deploy + smoke test · [P0] · depends: T12.5, T11.1

> **PROMPT**
> Goal: the first real deployment, with rule C1 checked first.
> Files you may touch: `docs/aws_live_notes.md` ("Deploy" section).
> Requirements:
> 1. Run `aws budgets describe-budgets` (read-only) and stop if the budget is missing. Re-check the concurrency quota.
> 2. Print the exact `sam deploy --guided --template cloud/template.yaml --profile semauto --region ap-south-1` command, wait for "go", run it, and have me confirm the SNS subscription email.
> 3. Smoke tests, each pasted:
>    - (a) `curl -s -XPOST "$ApiUrl/classify" -H 'content-type: application/json' -d '<one aware request from the trace>'` → 202 + `predicted_archetype`;
>    - (b) the item appears in `semauto-counts`;
>    - (c) after `service_s` seconds, the result row appears in `semauto-results`;
>    - (d) `aws lambda invoke` the controller with `{"run_id":"smoke","policy":"aware","sizing":"ceil","minute":10}` and then `aws lambda get-event-source-mapping --uuid <eks_gpu_reserved uuid>`, showing `ScalingConfig`;
>    - (e) a router cold-start duration from CloudWatch Logs (`REPORT ... Init Duration`).
> 4. Record the stack outputs in the notes, **with the account ID redacted**.
> **VERIFY:** the pasted outputs (a)–(e).

---

### T12.7 · Image parity in the cloud · [P1] · depends: T12.6

> **PROMPT**
> Goal: Addendum §13 "Image parity", proved against the deployed router rather than only locally.
> Files you may touch: `cloud/parity_check.py` (new), `docs/aws_live_notes.md`.
> Requirements: send 500 stratified requests from the seed-42 trace (balanced across archetypes, seeded) to the live API with `policy=aware` and `run_id=parity-*`. Compare each returned `predicted_archetype` with local `classify()` using the seed-42 features and the seed-42 model. Then delete the parity rows, or leave them to TTL and say so.
> **VERIFY:** paste the agreement (expect 500/500); any disagreement is a bug to fix before Phase 13.

---

### T12.8 · Governance enforcement (optional) · [P2] · depends: T12.6

> **PROMPT**
> Goal: make `throttle` actually do something, behind `EnforceGovernance=true`.
> Files you may touch: `cloud/handlers/router.py`, `cloud/handlers/controller.py`, `tests/cloud/test_router.py`, `tests/cloud/test_controller.py`, `TRD-AWS-Addendum.md` (§11.4, one paragraph documenting the rule).
> Requirements:
> - The controller writes `{run_id, budget_action}` to the decision item.
> - The router reads the run's latest action, cached for 10 s per container. Under `throttle` it returns `429` for `predicted_archetype == batch_offline` only, because batch work is latency-insensitive and deferrable, and records it as `deferred`. `downgrade_tier` stays advisory.
> - Tests cover both. Every live result reports whether enforcement was on.
> **VERIFY:** `pytest -q`; one live demonstration with a temporarily lowered budget parameter, pasted.

---

## PHASE 13 — Live experiment

### T13.1 · Replay driver · [P0] · depends: T12.6

> **PROMPT**
> Goal: `cloud/replay.py` exactly per Addendum §11.6.
> Files you may touch: `cloud/replay.py`, `tests/cloud/test_replay.py`, `.gitignore` (`data/live/*`, keeping `.gitkeep`).
> Requirements:
> 1. CLI: `python -m cloud.replay --policy {baseline,aware} --sizing {round,ceil} --time-scale K --minutes N [--dry-run]`. `--dry-run` prints the schedule (first 20 sends and the tick times) without touching AWS.
> 2. The schedule is built only from `generate_trace`, `add_proxy_features`, `service_seconds` and `arrival_seconds` with the same seeds as the simulation. `test_replay.py` asserts that the schedule's request IDs, minutes, offsets and service times equal the simulation's inputs exactly.
> 3. Send with a `ThreadPoolExecutor` (16 workers) and stdlib `urllib.request`, on a monotonic clock, recording the planned and actual send time, HTTP status and RTT per request. Log the lateness distribution; if the p99 lateness is > 0.5 s / K, warn loudly.
> 4. At each simulated-minute boundary ≥ `start_minute`, make a synchronous `lambda.invoke` of the controller (RequestResponse) and record the tick latency.
> 5. Before a run: purge queues (respect the 60 s `PurgeQueue` cooldown; check the docs), send reset and warmup, and sleep 30 s. After a run: wait for drain (visible + not-visible = 0 on all 5 queues), then export the three tables for the `run_id` (paginated `Query`) to `data/live/`.
> 6. Handle Ctrl-C gracefully: stop sending, still drain and export, and mark the run `aborted` in the driver CSV.
> **VERIFY:** `pytest tests/cloud/test_replay.py -v`; `python -m cloud.replay --policy aware --sizing ceil --time-scale 1 --minutes 120 --dry-run | head -30`.

---

### T13.2 · Calibration run: actuation lag → choose time_scale · [P0] · depends: T13.1

> **PROMPT**
> Goal: measure the infrastructure delays the simulation does not model, and fix `time_scale` **before** the real runs (rule H1).
> Files you may touch: `cloud/calibrate.py` (new), `docs/aws_live_notes.md` ("Calibration" section).
> Requirements:
> 1. Actuation lag: on the `eks_gpu_reserved` queue, preload 60 messages with `service_s=6` while `MaximumConcurrency=2`. Then set it to 8 and poll the worker's `ConcurrentExecutions` metric, or count overlapping `start_ts`/`end_ts` in results, to find when concurrency actually reaches 8. Repeat 5 times and report the median and p95.
> 2. Router overhead: the p50/p95 of `ingress_ts − driver send time`, as best measurable given clock skew, together with the router `Duration` from logs.
> 3. Polling delay: the p50/p95 of `start_ts − ingress_ts` when the pool is idle.
> 4. Rule: choose the largest K in {1, 2, 3, 4, 6} such that p95 actuation lag < 0.25 × 60/K seconds. Write the chosen K and the measurements to the notes. **K is now frozen for all Phase 13 runs.**
> **VERIFY:** paste the measurement table and the chosen K, with the arithmetic shown.

---

### T13.3 · The live runs · [P0] · depends: T13.2

> **PROMPT**
> Goal: real queueing-latency data for both policies under both sizing rules, on the same trace.
> Files you may touch: `docs/aws_live_notes.md` ("Runs" section) only. The data files are produced by `replay`.
> Requirements:
> 1. **Required minimum:** 4 runs at the frozen K: (baseline, ceil), (aware, ceil), (baseline, round), (aware, round). Alternate the order A-B-B-A to spread any time-of-day drift.
> 2. **P1:** repeat the two `ceil` runs twice more (6 `ceil` runs total) for run-to-run variance.
> 3. Before each run, print the command and the expected wall time (`120/K` min plus the drain), and wait for "go". After each run, paste the driver summary: sent, 2xx count, non-2xx count, results rows, and lateness p99.
> 4. Watch the CloudWatch dashboard during at least one aware run across the shift and tell me when to take the screenshots (§G checklist).
> 5. After every run, check the cost in Billing/Cost Explorer. If month-to-date spend exceeds USD 2, STOP and tell me.
> **VERIFY:** a table of runs: run_id, policy, sizing, K, sent, results, lost (must be 0), aborted?

---

### T13.4 · Live analysis + sim-vs-live validation · [P0] · depends: T13.3

> **PROMPT**
> Goal: `cloud/analyze_live.py`, which turns the live data into the comparison the report needs and checks the simulation against reality.
> Files you may touch: `cloud/analyze_live.py` (new), `tests/cloud/test_analyze_live.py` (new, on a small synthetic fixture), `README.md` ("Live AWS results" section).
> Requirements:
> 1. Per run, response time = `(end_ts − ingress_ts) × K` in simulated seconds. Compute the SLA flags against Addendum §9.1, and produce the same `summarise_latency` output as the simulation by reusing that function. Do not write a second summariser.
> 2. Rebuild the capacity timeline from `semauto-decisions` and run `simulate_policy_latency` on the same trace with that exact timeline, **using the live `predicted_archetype` from `semauto-results` for routing**, so that the only remaining difference is the infrastructure. That gives the live-vs-sim comparison: p50/p95 per pool, absolute and relative error, and SLA-violation rate live vs sim.
> 3. Governance: the first breach minute per policy and the lead time, live vs simulation.
> 4. Outputs: `data/live/summary.json`; `data/live/live_vs_sim.png` (per-pool p95, live dots vs sim bars; and capacity timelines live vs sim); `data/live/latency_timeline.png` (rolling SLA-violation rate, both policies, shift shaded).
> 5. README "Live AWS results": the run table, the latency table and the live-vs-sim table. Add one paragraph on where the simulation and reality diverge (cold starts, polling delay, actuation lag from T13.2) and whether the policy ranking survives in the live system. State the result as measured.
> **VERIFY:** `python -m cloud.analyze_live` (paste the summary); `pytest -q`; describe both PNGs in two sentences each.

---

## PHASE 14 — Teardown, cost audit, documentation

### T14.1 · Teardown + cost audit · [P0] · depends: T13.4

> **PROMPT**
> Goal: Addendum NFR-A2, which requires zero resources left and a recorded cost.
> Files you may touch: `docs/aws_live_notes.md` ("Teardown" and "Cost" sections).
> Requirements: print and wait for "go" before each command:
> 1. Export anything not yet exported.
> 2. `sam delete --stack-name semauto-live --region ap-south-1 --profile semauto`.
> 3. Delete the SAM-managed ECR repository images if `sam delete` leaves them; check the docs for whether it does.
> 4. Delete the `/aws/lambda/semauto-*` log groups.
> 5. Run the tagging-API query from Addendum §13 and the `aws cloudformation list-stacks` filter, and paste both empty results.
> 6. Twenty-four hours later, record month-to-date cost by service from Cost Explorer. The budget stays in place; delete it only if I ask.
> **VERIFY:** the pasted empty resource lists and the cost table.

---

### T14.2 · Final documentation pass · [P0] · depends: T14.1

> **PROMPT**
> Goal: a README and notes that let someone reproduce Part 2 from a fresh clone, and that state precisely what was and was not done.
> Files you may touch: `README.md`, `docs/aws_live_notes.md`, `docs/architecture_live.md` (new: a Mermaid diagram of the deployed system matching Addendum §11.5, plus the §11.1 real/emulated table verbatim).
> Requirements: a reproduce section for Phase 10 (`run_comparison`, `multiseed`, `sensitivity`) and for Phases 11–14 (prerequisites, deploy, calibrate, run, analyze, teardown, with expected wall time and cost); a "Limitations" list that includes at least: emulated serving pools; recalibration not deployed; single trace family; SLA values chosen a priori; and live run count. Remove any statement that the data no longer supports.
> **VERIFY:** follow the Phase 10 reproduce section from a clean copy of the repo and confirm the outputs match; `pytest -q`; `mypy src/`; `git status` clean after the commit.

---

# §E2. Build order

| Session | Tasks | Outcome | AWS cost |
|---|---|---|---|
| 10 | T10.0, T10.1 | Spec installed, golden frozen, sizing keyword | ₹0 |
| 11 | T10.2 | Validated queueing model | ₹0 |
| 12 | T10.3 | Latency results, both sizing rules | ₹0 |
| 13 | T10.4, (T10.5) | 30-seed significance, sensitivity | ₹0 |
| 14 | T11.1, T11.2 | Safe account, toolchain, quota request filed | ₹0 |
| 15 | T12.1–T12.4 | Image and handlers, all tested offline | ₹0 |
| 16 | T12.5, T12.6, (T12.7) | Deployed and smoke-tested | cents |
| 17 | T13.1, T13.2 | Driver; time_scale frozen | cents |
| 18 | T13.3, T13.4 | Live results and sim-vs-live | < USD 2 total |
| 19 | T14.1, T14.2 | Torn down, documented | ₹0 |

File the quota request (T11.1) as early as possible. It can take a while to be approved, and Phase 10 is good work to do while you wait.

---

# §F2. Final acceptance — "the project is 100% complete" when:

1. **Golden (§13):** with the default `round`, the evaluation is byte-identical to the Part 1 run. ✅
2. **Latency model (§13 a–e):** all five correctness tests pass. ✅
3. **Latency results:** `latency_summary.json` exists for both policies × both sizing rules, and the README explains the §8.1 finding. ✅
4. **Statistics:** 30 seeds × 2 sizing rules, with Wilcoxon results, CIs and effect sizes reported, whatever they show. ✅
5. **Offline tests:** `pytest -q` passes with no network and no AWS credentials; `mypy src/` is clean. ✅
6. **Live:** deployed under a pre-existing budget; smoke tests (a)–(e) pasted; at least the 4 required runs with zero lost requests. ✅
7. **Validation:** a live-vs-sim table exists, and the divergences are explained using the measured calibration numbers. ✅
8. **Teardown:** the tag query is empty; the final cost is recorded and under USD 5. ✅
9. **Honesty:** README Limitations matches Addendum §11.1 and §14; every result was produced with parameters frozen before the run. ✅

> Not part of 100%, and stated as future work in the report: the full STAR GAT+Transformer+ESRL policy, real GPU serving, and a deployed recalibration loop.

---

# §G. Screenshot checklist for the final report

Capture these during the work, because they cannot be recreated after teardown. Save them to `docs/screenshots/` and index them in `docs/aws_live_notes.md`.

1. The AWS Budgets page showing the USD 5 budget and its alert thresholds (T11.1).
2. Service Quotas: the Lambda concurrent executions value (T11.1).
3. The `sam deploy` outputs in the terminal, with the account ID blurred (T12.6).
4. The CloudFormation stack's Resources tab (T12.6).
5. The `curl /classify` request and response (T12.6).
6. Lambda → each worker → Configuration → Triggers, showing the SQS trigger with its maximum concurrency (T12.6).
7. **The CloudWatch dashboard during an aware run, about 10 simulated minutes after the shift.** This is the key figure: forecast and units moving while total requests stay flat (T13.3).
8. SQS queue monitoring: `ApproximateAgeOfOldestMessage` for `homogeneous_pool` vs `eks_gpu_reserved` across the shift (T13.3).
9. The DynamoDB `semauto-decisions` items for one run (T13.3).
10. The SNS budget-breach email (T13.3).
11. Cost Explorer by service, month to date (T14.1).
12. The empty tag-query output after teardown (T14.1).

---

# §H. Troubleshooting (known failure modes)

| Symptom | Likely cause | What to do |
|---|---|---|
| `sam build` fails on numpy/scipy wheels | No cp314 aarch64 wheel for a pin | Check PyPI for the pin's wheel list. If a wheel is genuinely missing, switch the **image only** to `python:3.13` with the same pins, if 3.13 wheels exist; log it in the notes and the README. Never bump pins silently. |
| `TooManyRequestsException` on router or workers | Account concurrency quota too low (NFR-A3) | Check the quota; use the T11.1 fallback (half rate) and log it. |
| Concurrency never exceeds about 5 per pool | ESM poller scaling, not your MaximumConcurrency | Measured in T13.2; that is why K is chosen from data. Report it; don't work around it. |
| Messages reappear and are processed twice | Visibility timeout < worker timeout | Template test enforces ≥ 6×; check that the deployed value matches. |
| Router cold start of 5–10 s distorts the first minute | Large image import | Warmup event before each run (T13.1). Report cold starts separately. |
| Live latency much higher than sim everywhere | Polling and actuation delay | Expected direction. Quantify with T13.2 numbers in T13.4; don't retune K after the fact (H1). |
| Budget alert email arrives | Something is still running | Run the T14.1 tag query immediately and tear down. |

**This part exists to answer the same question as Part 1, honestly and at full strength: does knowing what kind of request is coming make autoscaling measurably better? Part 2 answers it with latency rather than capacity, across 30 seeds rather than one, and on real AWS infrastructure rather than only in simulation.**
