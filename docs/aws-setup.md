# AWS setup for M1

What has to exist before `halo quote "..."` makes a real call, in the order it
has to exist. Roughly fifteen minutes, and the finished setup costs nothing while
idle — Bedrock on-demand bills per token, with no standing charge.

Run `halo doctor` at any point. It checks each of these in order and stops at the
first thing that is missing, so you can work down the list rather than guess.

---

## 1. An AWS account and an identity to call with

You need credentials that can be resolved by the standard AWS chain. Two routes;
pick one.

**IAM Identity Center (recommended).** Short-lived credentials that expire, so a
leaked key is a smaller problem. Slightly more setup.

```bash
brew install awscli
aws configure sso          # SSO start URL, region, then pick the account/role
aws sso login --profile halo
export AWS_PROFILE=halo
```

**IAM user with an access key.** Faster, and acceptable for a practice account.

The access key is something you *create* — there is nothing to look up, and the
secret is shown exactly once. It is not your account number and not your console
password.

1. **IAM → Users → Create user.** Name it `halo-quote-copilot`. Do not give it
   console access; this identity only makes API calls.
2. On permissions, **Attach policies directly → Create inline policy → JSON**,
   and paste the policy from step 3 below. Skipping this produces a key that
   authenticates fine and gets `AccessDeniedException` on every Bedrock call.
3. Open the user → **Security credentials** → **Access keys → Create access key**.
4. Use case **Command Line Interface (CLI)**, acknowledge, Create.
5. Copy both values, or download the CSV. Lose the secret and you delete the key
   and make another — it cannot be retrieved.

The key id starts `AKIA` and is 20 characters; the secret is 40.

```bash
brew install awscli
aws configure              # key, secret, region us-east-1, output json
```

> Never create access keys on the **root** account. A leaked root key
> compromises everything on the account, billing included. The IAM user above is
> the right identity for this.

Either way, confirm the identity resolves:

```bash
aws sts get-caller-identity
```

> **Do not** put credentials in the repo. The `.env` and `*.tfstate` patterns are
> already in `.gitignore`; keys belong in `~/.aws/` or the environment.

---

## 2. Region

Use **us-east-1** unless you have a reason not to — it carries the widest Bedrock
model and feature availability, and the plan's defaults assume it.

```bash
export AWS_REGION=us-east-1
```

The CLI reads `AWS_REGION` and also takes `--region`.

---

## 3. IAM permissions

The calling identity needs to invoke Anthropic models and to list what is
available. Attach this as an inline policy — it is narrower than the AWS managed
Bedrock policies, which grant far more than this project uses.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeAnthropicModels",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*:*:inference-profile/*.anthropic.*"
      ]
    },
    {
      "Sid": "DiscoverModels",
      "Effect": "Allow",
      "Action": [
        "bedrock:ListFoundationModels",
        "bedrock:GetFoundationModel",
        "bedrock:ListInferenceProfiles"
      ],
      "Resource": "*"
    }
  ]
}
```

Two things about those resource ARNs that cause most of the "access denied"
confusion:

- Foundation-model ARNs carry **no account id** — `arn:aws:bedrock:*::foundation-model/...`,
  with the double colon. That is correct, not a typo.
- If the model resolves to a **cross-region inference profile** (an id beginning
  `us.`), you need invoke permission on both the profile *and* the underlying
  foundation models in every region the profile can route to. The wildcard region
  in the policy above covers that.

---

## 4. Model access

**The Model access page is retired.** Serverless foundation models now enable
themselves on first invocation. Two things still gate Anthropic models, and
neither is on that page.

### 4a. The use case details form

First-time users of Anthropic models must submit use case details once, for the
whole account. Until then every Anthropic model fails with:

> Model use case details have not been submitted for this account.

Submit it through the console: **Bedrock → Model catalog →** a Claude model **→
Open in Playground**. The form appears before the first message.

You can check the state from the CLI without touching the console:

```bash
aws bedrock get-use-case-for-model-access --region us-east-1
```

`ResourceNotFoundException: You have not filled out the request form` means it is
still outstanding. There is a `put-use-case-for-model-access` API, but it takes an
opaque `formData` blob whose contents are your company and use-case details — fill
it in yourself rather than have it generated.

### 4b. Marketplace enablement, done by an admin

For models served through AWS Marketplace, **a user with AWS Marketplace
permissions must invoke the model once** to enable it account-wide. The narrow
IAM policy in step 3 deliberately does not include those permissions.

So the first invocation has to come from an **administrator**, not from the IAM
user this project uses. After it, the narrow user can invoke normally.

Either identity works:

- **The account root user, or any identity with `AdministratorAccess`.** Simplest;
  nothing to undo afterwards.
- **This project's IAM user, temporarily.** Attach the AWS managed policy
  `AWSMarketplaceManageSubscriptions` to it, do the playground step, then detach
  it. Keeps the standing policy narrow while still allowing the one-time
  subscribe.

Console walkthrough, once signed in as that identity:

1. Set the region selector (top right) to **US East (N. Virginia)**.
2. **Amazon Bedrock → Model catalog** in the left navigation.
3. Filter provider **Anthropic** and pick the model — `Claude Sonnet 4.6` for
   this project.
4. **Open in Playground.** On first use the Anthropic use case details form
   appears; fill in the company and intended-use fields and submit.
5. Send any message — "hi" is enough. *That* call is the enablement; opening the
   page is not.
6. Repeat step 3-5 for any other model you want enabled. Worth trying
   `Claude Sonnet 5` while you are there: if it is not offered to the account it
   will say so here too, which settles whether this project stays on 4.6.

The agreement can also be accepted through the API, if you would rather not use
the console:

```bash
aws bedrock list-foundation-model-agreement-offers \
  --model-id anthropic.claude-sonnet-4-6 --region us-east-1
# then, with the offerToken from that response:
aws bedrock create-foundation-model-agreement \
  --model-id anthropic.claude-sonnet-4-6 --offer-token <token> --region us-east-1
```

That accepts a commercial agreement on the account's behalf, so run it knowingly.

### Which refusal is which

| Message | Meaning |
|---|---|
| `use case details have not been submitted` | Step 4a. Affects every Anthropic model; no id works around it. |
| `<model> is not available for this account` | That model tier is not offered to the account. 4a and 4b will not change it — use a model the entitlement check passes for. |

`ListFoundationModels` shows the region's whole catalogue regardless of what the
account may call, so a model appearing there proves nothing. `halo doctor` uses
`GetFoundationModelAvailability`, and only its final check — a real call — is
conclusive.

## 5. A spend guardrail

You chose cost-conscious, so set this before the first call, not after the first
surprise.

- **Billing → Budgets → Create budget**, monthly cost budget, a figure you would
  not mind losing (US$20 is ample for M1–M4), with an alert at 50% and 100%.
- Bedrock on-demand has no idle cost. Nothing in M1 provisions capacity, so the
  only way this bill grows is calls you make.

For reference, an M1 draft is roughly 1,500 input and 800 output tokens — a
fraction of a cent per run at Sonnet rates.

---

## 6. Validate

```bash
halo doctor
```

Checks credentials, region, model availability and then makes one deliberately
tiny call, reporting the tokens and estimated cost. When it passes:

```bash
halo quote "Customer wants 500 hoodies, 3-colour front print, Chicago by Oct 15, budget \$12k."
```

Expect exit code **2** and an escalation. That is M1 working. The draft will look
entirely credible and every figure in it is invented — which is the baseline M2
and M3 are measured against.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No AWS credentials found` | Nothing in the credential chain | `aws configure`, or `export AWS_PROFILE=...` |
| `ExpiredToken` / `The security token included in the request is expired` | SSO session lapsed | `aws sso login --profile halo` |
| `is not available to this account in <region>` | Model access not enabled, or wrong id | Step 4, then pass `--model` with an id from the list |
| `may not invoke ... AccessDeniedException` | IAM policy missing, or foundation-model ARN written with an account id | Step 3 — note the double colon |
| `ValidationException` naming an inference profile | Model requires a profile id | Use the `us.`-prefixed id from `list-inference-profiles` |
| `ThrottlingException` | Account quota | Retry; request a quota increase in Service Quotas if it persists |

## Cost note

`PRICE_PER_MTOK` in `src/halo/platform/bedrock.py` currently holds first-party
Anthropic rates. Bedrock is partner-operated and prices separately. The numbers
drive the run budget, not a bill, but correct them from the Bedrock pricing page
once the account is live so the ceiling means what it says.
