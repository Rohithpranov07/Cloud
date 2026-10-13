# AWS live-deployment notes

Running log for Phase 11–14 (Build-Instructions-Part2.md). Budget, quota, deploy, run and
teardown decisions are recorded here, plus the screenshot index (§G). No account IDs,
ARNs, emails, or keys are ever written to this file (NFR-A6) — redact before pasting.

---

## T11.1 — Account safety: budget, MFA, CLI profile, quota

**Status: account created, root MFA on, IAM user `semauto-admin` created with
AdministratorAccess, CLI profile `semauto` configured for `ap-south-1`, budget created
and verified, quota increase requested and pending. Only the Free Tier status check and
the (deferred) cost-allocation tag remain before this task is complete.**

### Account sign-up (prerequisite — no AWS account existed yet)

0. **Create the AWS account itself.**
   Docs: [AWS Account Management](https://docs.aws.amazon.com/accounts/latest/reference/manage-account-payment.html).
   - Go to [aws.amazon.com](https://aws.amazon.com) → **Create an AWS Account**.
   - Enter an email address (this becomes the **root user** login) and an account name,
     verify the email with the code AWS sends, and set a root password. Use a real email
     you control and a strong, unique password saved in a password manager — this is the
     most privileged login on the account.
   - Choose **Personal** account type, enter your name/phone/address.
   - Add a payment method (a card is required even to stay on the free tier — AWS places
     a small temporary ~$1 verification hold, not a real charge).
   - Complete phone verification (SMS or automated call with a PIN).
   - Select the **Basic support plan** (free) — not Developer/Business/Enterprise.
   - Accept the agreement and submit. Activation is usually a few minutes.
   - **Immediately after activation**, do the MFA step below before anything else.

### Identity

1. **Turn on MFA for the root user.**
   Docs: [AWS account root user](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_root-user.html) /
   [Multi-factor authentication for AWS account root user](https://docs.aws.amazon.com/IAM/latest/UserGuide/enable-mfa-for-root.html).
   AWS now enforces MFA for root users by default, but it requires you to actually add an
   MFA device during account creation or the next time you sign in.
   - Sign in to the [AWS Management Console](https://console.aws.amazon.com) as the root
     user (email + password, not an IAM user).
   - Top-right account menu → **Security credentials**.
   - Under **Multi-factor authentication (MFA)**, choose **Assign MFA device**.
   - Pick a virtual MFA app (e.g. an authenticator app) or a hardware/passkey device, and
     follow the wizard to pair and confirm it.

2. **Create an administrative identity — never use root access keys.**
   Docs: [Getting started with IAM Identity Center](https://docs.aws.amazon.com/singlesignon/latest/userguide/getting-started.html).
   AWS's current guidance is explicit: *"We recommend that you configure an administrative
   user in AWS IAM Identity Center to perform daily tasks... you should not use the root
   user for your everyday tasks."* Two paths, pick ONE:

   - **(Preferred) IAM Identity Center**, if you're comfortable with the extra setup:
     1. Console → **IAM Identity Center** → **Enable**. For a single personal/course
        account (no AWS Organization), choose an **account instance**, not an
        organization instance.
     2. Use the Identity Center directory as the identity source (no external IdP needed)
        — see [Create and manage users directly in IAM Identity Center](https://docs.aws.amazon.com/singlesignon/latest/userguide/quick-start-default-idc.html).
     3. Create a user for yourself, create or reuse the **AdministratorAccess** permission
        set, and assign it to that user for this account.
     4. Note your **AWS access portal URL** (`https://<your-subdomain>.awsapps.com/start`
        or similar) — you'll need it for `aws configure sso` below.

   - **(Simpler fallback for a solo course-project account) a plain IAM user**, explicitly
     allowed by the build instructions if Identity Center feels like overkill for one
     person:
     1. Console → **IAM** → **Users** → **Create user**.
     2. Username e.g. `semauto-admin`. Do **not** enable console access unless you want
        it; for this project you mainly need programmatic (CLI) access.
     3. Attach the AWS-managed policy **AdministratorAccess** directly (acceptable for a
        single-developer course project; least-privilege per-service policies are the
        production-grade version of this step, out of scope here).
     4. On the user's **Security credentials** tab → **Create access key** → choose
        **Command Line Interface (CLI)** as the use case → create it. You'll be given an
        Access Key ID and Secret Access Key **once** — save them somewhere safe (a local
        password manager, not this repo).

3. **Configure the CLI profile `semauto`, region `ap-south-1`.**
   Docs: [Configuring IAM Identity Center authentication with the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html) /
   [aws configure sso — AWS CLI v2 reference](https://awscli.amazonaws.com/v2/documentation/api/latest/reference/configure/sso.html).

   - If you created an **IAM Identity Center** user:
     ```
     aws configure sso
     ```
     When prompted: SSO session name → anything, e.g. `semauto`; SSO start URL → your
     access-portal URL from step 2; SSO region → the region IAM Identity Center is hosted
     in (shown on its console page — may differ from `ap-south-1`, that's fine, it's just
     where the directory lives); registration scope → `sso:account:access`; pick the
     account and the `AdministratorAccess` permission set when prompted; default Region →
     `ap-south-1`; default output → `json`; profile name → `semauto`.
     Then run `aws sso login --profile semauto` whenever the cached session expires.

   - If you created a plain **IAM user**:
     ```
     aws configure --profile semauto
     ```
     Paste the Access Key ID / Secret Access Key from step 2; default region name →
     `ap-south-1`; default output format → `json`.

### Budget

4. **Create (or verify) a monthly USD 5 cost budget with alerts at 50%, 80%, 100% actual
   and 100% forecast, emailed to you.**
   Docs: [Creating a budget](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html) /
   [Customizing a budget (advanced)](https://docs.aws.amazon.com/cost-management/latest/userguide/custom-budgets.html) /
   [Adding email recipients to a budget notification](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-email-recipients.html).
   If Part 1's T9.1 already created a budget, open it and verify these same settings
   instead of creating a second one.
   - Console → **Billing and Cost Management** → **Budgets** → **Create budget**.
   - Budget type: **Cost budget**, **Customize (advanced)** so every threshold below is
     configurable individually.
   - Period: **Monthly**, recurring, no end date. Budgeted amount: **$5.00**.
   - Add FOUR alert thresholds (AWS Budgets lets you add each as a separate
     notification): 50% of **actual** spend, 80% of **actual** spend, 100% of **actual**
     spend, and 100% of **forecasted** spend. For each, add your email as the
     notification recipient — you'll get a confirmation email you must click to verify
     the subscription before alerts actually deliver.
   - Note: AWS needs about five weeks of usage history to produce reliable forecasts, so
     the forecast-based (100%) alert may not fire meaningfully in week one — that's
     expected, not a misconfiguration.

5. **Turn on the `project` cost-allocation tag.**
   Docs: [Activating user-defined cost allocation tags](https://docs.aws.amazon.com/cost-management/latest/userguide/activate-user-defined-tags.html).
   - Console → **Billing and Cost Management** → **Cost Allocation Tags**.
   - It only appears under **User-defined tags** once at least one resource carrying that
     tag exists — it won't show up until after `cloud/template.yaml` (Phase 12) tags its
     first resource with `project=semantic-autoscaling`, and then it needs attention
     (**Activate**) plus up to 24h propagation delay. **Come back to this step after
     T12.5/T12.6**; noting it now so it isn't forgotten.

### Free-tier status

6. **Check the Free Tier status page.**
   Docs: [AWS Free Tier](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/billing-free-tier.html).
   - Console → **Billing and Cost Management** → **Free Tier** (left nav, or
     Billing Preferences has a "Free Tier alerts" opt-in too — turn that on while you're
     there).

   **Checked 2026-10-13: account is on the free plan, $100 credit remaining.** Comfortably
   covers this project's expected <USD 2 live-AWS spend many times over; the credit is a
   backstop, not a reason to relax the $2 ceiling or the teardown discipline (C3).

### Verify (read-only) — real output, 2026-10-13

```
$ aws sts get-caller-identity --profile semauto
{
    "UserId": "<redacted>",
    "Account": "<ACCOUNT_ID>",
    "Arn": "arn:aws:iam::<ACCOUNT_ID>:user/semauto-admin"
}
```
Confirms: the plain-IAM-user path (not Identity Center), user `semauto-admin`, profile
`semauto` working.

```
$ aws budgets describe-budgets --account-id <ACCOUNT_ID> --profile semauto
{
    "Budgets": [{
        "BudgetName": "semauto",
        "BudgetLimit": {"Amount": "5.0", "Unit": "USD"},
        "TimeUnit": "MONTHLY",
        "CalculatedSpend": {"ActualSpend": {"Amount": "0.0", "Unit": "USD"}},
        "BudgetType": "COST",
        "HealthStatus": {"Status": "HEALTHY"}
    }]
}
```

```
$ aws budgets describe-notifications-for-budget --account-id <ACCOUNT_ID> --budget-name semauto --profile semauto
{
    "Notifications": [
        {"NotificationType": "ACTUAL",     "Threshold": 100.0, "NotificationState": "OK"},
        {"NotificationType": "ACTUAL",     "Threshold": 50.0,  "NotificationState": "OK"},
        {"NotificationType": "ACTUAL",     "Threshold": 80.0,  "NotificationState": "OK"},
        {"NotificationType": "FORECASTED", "Threshold": 100.0, "NotificationState": "OK"}
    ]
}
```
All four required thresholds present (50/80/100% actual, 100% forecast), all `OK`
(none breached). Budget step verified complete.

```
$ aws service-quotas get-service-quota --service-code lambda --quota-code L-B99A9384 --region ap-south-1 --profile semauto
{
    "Quota": {
        "ServiceCode": "lambda",
        "QuotaCode": "L-B99A9384",
        "QuotaName": "Concurrent executions",
        "Value": 10.0,
        "Adjustable": true,
        "QuotaAppliedAtLevel": "ACCOUNT"
    }
}
```
Confirms the quota code is exactly "Concurrent executions", as required before trusting
it. Applied value: **10** — below the NFR-A3 minimum of 50.

### Concurrency quota

NFR-A3 requires >= 50 in `ap-south-1` before T12.6. Starting value was **10** (new
account default). Requested an increase via:
```
$ aws service-quotas request-service-quota-increase --service-code lambda --quota-code L-B99A9384 --desired-value 1100 --region ap-south-1 --profile semauto
{
    "RequestedQuota": {
        "Id": "d1e50a11d1964b409d2345fc1f63b9061J7Srii4",
        "QuotaCode": "L-B99A9384",
        "DesiredValue": 1100.0,
        "Status": "PENDING",
        "Created": "2026-10-13T13:31:34.385000+05:30"
    }
}
```
Request ID: `d1e50a11d1964b409d2345fc1f63b9061J7Srii4`, filed 2026-10-13, desired value
1100 (well above the 50 minimum — harmless if granted in full, and the minimum is still
met if AWS grants a smaller increase). **Status PENDING as of filing; must be re-checked
before T12.6** (`aws service-quotas get-service-quota ...` again) — small increases like
this are often auto-approved within minutes to a few hours, but not guaranteed.

- **Fallback if still denied/pending by T13**: halve `base_rate_per_min` to 25 for both
  the simulation and the live run, log it here, and re-run T10.3 at that rate so live and
  simulated results stay comparable (per the Build Instructions' documented fallback).

### Decisions

- Used the plain-IAM-user path (`semauto-admin` + `AdministratorAccess`) instead of IAM
  Identity Center — simpler for a solo course-project account, explicitly allowed as a
  fallback by the Build Instructions.
- User-set hard cost ceiling for this project's entire live-AWS portion (Phases 11-14):
  **USD 2 total**, confirmed 2026-10-13. This matches (doesn't tighten) the Build
  Instructions' own NFR-A1 estimate, but is treated as a hard stop: check Cost Explorer
  spend against it during Phase 13, not just the $5 budget's own alert thresholds.

---

## T11.2 — Toolchain: AWS CLI v2, SAM CLI, Docker (arm64)

**Status: complete. AWS CLI, SAM CLI, Docker (arm64) and boto3 all verified. Docker
Desktop crashed mid-task when the host disk filled; it was recovered after the user freed
space (see "Docker Desktop — disk-space crash and recovery" below).**

| Tool | Version | How installed |
|---|---|---|
| AWS CLI v2 | `aws-cli/2.37.13 Python/3.14.6 Darwin/25.2.0 exe/arm64` | pre-installed by the user (T11.1) |
| AWS SAM CLI | `SAM CLI, version 1.168.0` | official AWS `.pkg`, current-user install, no sudo (see below) |
| Docker Desktop | server arch `arm64` | pre-installed by the user |
| boto3 | `1.43.112` (latest on PyPI, checked 2026-10-13) | `requirements-cloud.txt` → `.venv`; `botocore.stub.Stubber` imports |

### SAM CLI — why the official package, not Homebrew

Docs: [Install the AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) /
[Managing AWS SAM CLI versions](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/manage-sam-cli-versions.html).

1. First tried the documented community formula, `brew install aws-sam-cli`. It installed,
   but `sam validate` didn't exist ("No such command 'validate'"). The formula runs on
   Homebrew's `python@3.14`, which that same install upgraded to **3.14.8_2**. That
   bottle's `pyexpat` links `/usr/lib/libexpat.1.dylib` and needs
   `_XML_SetAllocTrackerActivationThreshold`, which macOS 26.2's system libexpat doesn't
   export (the bottle targets a newer macOS). `brew update` showed no fixed rebuild yet.
2. Uninstalled it and used AWS's recommended `.pkg` instead (it bundles its own Python):
   downloaded `aws-sam-cli-macos-arm64.pkg`; checked with `pkgutil --check-signature`
   (signed "Developer ID Installer: AMZN Mobile LLC (94KV3E626L)", notarized by Apple);
   installed via the docs' "Command line – Current user" method to
   `~/.local/share/aws-sam-cli`; symlinked into `~/.local/bin/sam` (already on PATH).
   `sam validate --lint` works offline.

### Project `.venv` — repaired after the same Homebrew upgrade

The `.venv` was created on Homebrew Python **3.14.7**, but its interpreter symlink
resolved through `/opt/homebrew/opt/python@3.14`, which now points at the broken
3.14.8_2. Symptoms: `platform.mac_ver()` silently returned `''` (plistlib can't import
pyexpat), so pip crashed in both `truststore` and its wheel-tag code. Fixed by pointing
`.venv/bin/python3.14` and `pyvenv.cfg`'s `home` at the still-installed, healthy
`/opt/homebrew/Cellar/python@3.14/3.14.7`. The original config is saved as
`.venv/pyvenv.cfg.bak-3.14.8-breakage`. Tests (280 passed) and `mypy src/` are unaffected.

**Fragile: don't run `brew cleanup` (or let it auto-run) while this workaround is in
place.** It would delete the 3.14.7 keg that the venv now depends on. Durable fixes,
whichever comes first: a Homebrew rebuild of `python@3.14` that works on macOS 26.2, or
a macOS update. After either, recreate the venv normally.

### Verify (real output, 2026-10-13)

```
$ aws --version
aws-cli/2.37.13 Python/3.14.6 Darwin/25.2.0 exe/arm64
$ sam --version
SAM CLI, version 1.168.0
$ docker version --format '{{.Server.Arch}}'
arm64
$ python -c "import boto3, botocore.stub; print(boto3.__version__)"
1.43.112
$ pytest -q
280 passed
$ mypy src/
Success: no issues found in 21 source files
```

### Docker Desktop — disk-space crash and recovery

Partway through this task the host data volume hit 99% (228 GiB total, 3.4 GiB free) and
Docker Desktop's VM crashed: `write .../log/vm/console.log: no space left on device`. The
backend then stuck in a `stopping` state that neither a GUI quit nor
`docker desktop restart` could clear ("processes still running ... context deadline
exceeded"). Recovery:

1. The user freed space (to ~9.4 GiB free) and quit Docker Desktop.
2. The leftover hung processes (`com.docker.backend` and the sandbox daemon) were
   stopped. One needed SIGKILL after SIGTERM didn't end it.
3. `docker desktop start` brought the engine back (`/_ping` → `OK`). `arm64` re-verified.

**Disk headroom is still tight (~9 GiB free).** Phase 12 builds a ~1–1.5 GB arm64 Lambda
image (numpy, pandas, scipy, scikit-learn, statsmodels) plus build layers. Check
`df -h /System/Volumes/Data` before T12.1, and run `docker system prune` after builds if
space gets low.

---

## T12.6 — First deploy + smoke tests (2026-10-13)

Stack `semauto-live` in `ap-south-1`, deployed by the user from their own terminal with
`sam build` + `sam deploy --parameter-overrides AlertEmail=<redacted>` (interactive change-set
review). Preconditions checked read-only first: rule C1 (budget `semauto` $5.00 exists, healthy);
Lambda concurrency quota **10** (request `d1e50a11...Srii4` still `CASE_OPENED`, not yet approved —
enough for single-request smoke tests, **not** for Phase 13).

### Stack outputs (account ID redacted)

| Output | Value |
|---|---|
| ApiUrl | `https://<api-id>.execute-api.ap-south-1.amazonaws.com` (route `POST /classify`) |
| ControllerFunctionName | `semauto-controller` |
| Queues | `https://sqs.ap-south-1.amazonaws.com/<ACCOUNT>/semauto-{bedrock_on_demand,sagemaker_endpoint,eks_gpu_reserved,eks_gpu_spot,homogeneous_pool}` |
| Tables | `semauto-counts`, `semauto-decisions`, `semauto-results` |

Verified against the template (read-only): `CREATE_COMPLETE`, 33 resources (8 IAM roles, 7 functions,
5 queues, 5 mappings, 3 tables, schedule, topic, subscription, dashboard, API + stage, permission). No
EC2/SageMaker/EKS. 2 image functions (1024 MB; 10 s / 60 s) + 5 zip workers (128 MB, 30 s), all arm64.
Every mapping: `BatchSize 1`, `MaximumConcurrency 2`, `ReportBatchItemFailures`. Schedule `DISABLED`.
Email subscription confirmed.

The deploy also creates two stacks outside `semauto-live`: `semauto-live-<hash>-CompanionStack` (two
ECR repos, ~330 MB image) and the shared `aws-sam-cli-managed-default` (S3 upload bucket). **Teardown
(T14.1) must delete all three**, not just `semauto-live`.

### Smoke tests

**(a) `POST /classify`**, one correctly-classified agentic request from the seed-42 trace (id 5, run
`smoke-api`):
```
HTTP 202  {"request_id": 5, "predicted_archetype": "agentic_tool_using",
           "confidence": 0.9739, "pool": "eks_gpu_reserved"}
```
The **first attempt returned HTTP 500 after 20.8 s** (cold start, see below); the retry returned 202.

**(b) counter written** — `semauto-counts[smoke-api, 0]`: `agentic_tool_using = 1`.

**(c) worker result** — `semauto-results[smoke-api, 5]`: `service_s 10.765728`, slot held
`end_ts - start_ts = 10.766 s` (exact), `start_ts - ingress_ts = 2.132 s` (SQS polling + worker
cold start; real infrastructure delay the simulation does not model), response `12.898 s`.

**(d) controller, live actuation.** At the task's literal minute 10 every pool is at 1 unit
(= concurrency 2 = the reset value), so a tick there cannot change anything; verified locally with
`simulate(recalibrate=False)`. The first minute that moves a pool is **70** (aware/ceil:
`eks_gpu_reserved` 1 -> 2 units). So run `smoke-ctl` was seeded with the real per-minute counts for
minutes 0-70 (71 items, 3,512 requests) and ticked at minute 70:
```
changes: {"eks_gpu_reserved": 4}   capacity: bedrock 1, sagemaker 1, eks_gpu_reserved 2, eks_gpu_spot 1
projected_spend 14.79   budget_action none   breach_projected false   notified false
```
`aws lambda get-event-source-mapping` then reported `State Updating` -> `Enabled` with
`MaximumConcurrency 4` for `eks_gpu_reserved` (5-10 s later); the other four pools stayed at 2.
This matches the offline parity test's prediction. The recorded `semauto-decisions` item carries every
§11.3 field; the agentic forecast peak was 10.80 req/min (> 10 = one unit, hence 2 units under `ceil`).
`{"reset": ...}` then returned every pool to 2 (verified).

**(e) cold starts.** Router (1024 MB, arm64, container image):

| Case | Result |
|---|---|
| Cold start #1 | init hit Lambda's 10 s init-phase limit (`INIT_REPORT Init Duration 9999.50 ms, Status timeout`), was retried inside the invocation and timed out again at the 10 s function limit: **HTTP 500 after 20.8 s**, max memory 177 MB |
| Cold start #2 | **HTTP 202 after 18.1 s**: ~16.5 s from the first import log line to `START`, then 1.3 s in the handler. REPORT line carries no `Init Duration` field because init exceeded the limit |
| Warm | handler **14-52 ms**; **0.18-0.27 s** end to end from the user's laptop |

Controller (1024 MB, heavier imports incl. statsmodels): `{"warmup": true}` cold = **33 s** (function
timeout is 60 s, so it succeeds); the following warm tick took 3 s end to end.

### Consequence for Phase 13 — needs a decision before T13.1

A router container that starts cold takes ~17-21 s to become ready, which exceeds the router's 10 s
timeout (Addendum §11.5 fixes 1024 MB / 10 s). Any live request that lands on a cold container returns
HTTP 500, and the §13 live-run gate is **zero lost requests**. Warm containers are fine (milliseconds).
Mitigations, none applied yet:
1. **Pre-warm** the router in T13.1 with several concurrent dummy `POST /classify` calls (they return
   400 but initialise a container each), plus retry-on-5xx in the replay driver, logged and reported.
   Within the spec; no template change. The router needs well under one concurrent container at the
   experiment's ~5 req/s peak, so a handful of warm containers is ample.
2. Raise router memory (Lambda CPU scales with memory; init is CPU/IO-bound). Deviates from §11.5.
3. Not available: provisioned concurrency (rule C2), SnapStart (not supported for container images).

Also for T13.2: ESM concurrency changes took 5-10 s to apply, and the first worker poll took 2.1 s.

### Decisions

- Smoke-test run IDs `smoke-api`, `smoke-warm`, `smoke-ctl` leave small rows in the tables
  (TTL 7 days); teardown deletes the tables.
- Spend after deploy and all smoke tests: **$0.00** on the budget page (billing data lags up to ~24 h);
  the stack idles at ~$0.002/day (ECR storage). Total so far is far below the user's USD 2 ceiling.

---

## Phase 13 — preparation (2026-10-14)

**Decision (user, 2026-10-14): keep the `semauto-live` stack up for Phase 13** instead of tearing
it down after T12.6 (rule C3 exception, logged here). Idle cost is ~$0.002/day (ECR storage); the
three stacks (`semauto-live`, its `CompanionStack`, `aws-sam-cli-managed-default`) must still all be
deleted in T14.1.

### Built and tested offline (no AWS, $0)

| Task | File | Offline status |
|---|---|---|
| T13.1 | `cloud/replay.py` | 31 tests; `--dry-run` verified; schedule equals `simulate()`'s inputs bit for bit |
| T13.2 | `cloud/calibrate.py` | 28 tests; measurement recovers a simulated actuation lag exactly; K rule checked at every boundary. **Live measurement not yet run** |
| T13.4 | `cloud/analyze_live.py` | 12 tests on a synthetic run with a known 0.6 s shift; README "Live AWS results" waits for real data |

Two rule corrections, both from reading the docs: after a queue purge the driver waits **60 s**, not
the 30 s in the task text (SQS `PurgeQueue` docs: deletion takes up to 60 s, wait 60 s); and Lambda
invokes use `total_max_attempts=1` so a slow controller tick can never run twice.

### Why the full rate cannot run on this account (quota still 10)

`NFR-A3` wants a Lambda concurrency quota >= 50; it is **10** (request `d1e50a11...Srii4` is
`CASE_OPENED`; the Support API is not available on the Basic plan, so only the console's Support
Center shows AWS's reply). Peak slots in use at once, from the simulator
(`simulate(recalibrate=False)`, K does not change this because arrivals and service times scale together):

| trace rate | sizing | policy | cap slots (peak) | busy slots (peak) |
|---|---|---|---|---|
| 50 | round | aware / baseline | 10 / 6 | 10 / 6 |
| 50 | ceil | aware / baseline | 12 / 6 | 12 / 6 |
| 25 | round | aware / baseline | 8 / 2 | 8 / 2 |
| 25 | ceil | aware / baseline | 10 / 4 | 8 / 4 |

At 50 req/min the aware policy alone needs 10-12 workers at once before counting the router and
controller, which exceeds 10 and would throttle (lost requests). At **25 req/min** it needs 8, so the
documented T11.1 fallback (`base_rate_per_min` halved to 25 for the simulation AND the live run,
everything else unchanged) fits, tightly: ~8 busy workers + the router + a controller tick = ~10.

The budget stays at 24.0 under the fallback ("everything else unchanged"); scaling it to 12 would make
the aware policy breach at minute 10 because its one-unit-per-pool floor keeps spend near 0.2/min
regardless of rate. Simulated first projected breach at budget 24 / rate 25: aware+ceil minute 88,
aware+round none, baseline none (own-spend definition).

The fallback needs a pre-agreed rate in the **simulation** too (T10.3 re-run at 25): `replay` and
`analyze_live` already take the rate from the run's own metadata, and `analyze_live` simulates at it.
One known mismatch to report: the live router's classifier was trained at image-build time on the
rate-50 seed-42 trace, while a rate-25 simulation trains its own; `analyze_live` therefore routes the
simulation by the LIVE predictions, which removes the effect from the live-vs-sim comparison.

### Cost check for the live runs (rule C1 / the user's USD 2 ceiling)

Per run (~3,000 requests at rate 25): worker ~3,300 GB-s + router ~90 + controller ~220 GB-s, i.e.
~3,600 GB-s (free tier 400,000 GB-s/month); ~6,000 DynamoDB writes and ~20,000 read units
(on-demand, ~$0.01 a run); SQS/API Gateway far inside the free tiers. Custom metrics: CloudWatch
pricing (aws.amazon.com/cloudwatch/pricing) says they are *"prorated by the hour"* and charged only
in hours when data is sent, $0.30 per metric-month, 10 free; each run adds ~14 metrics for 1-2 hours,
i.e. cents. **Estimated Phase 13 total: well under $0.50**, versus the USD 2 ceiling.
