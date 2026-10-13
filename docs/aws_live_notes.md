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
