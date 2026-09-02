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
Create the user in the IAM console, attach the policy from step 3, create an
access key of type *Command Line Interface*, then:

```bash
brew install awscli
aws configure              # key, secret, region us-east-1, output json
```

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

Bedrock gates which foundation models an account may call, per region.

1. Open the Bedrock console in **us-east-1**.
2. Go to **Model access** in the left navigation.
3. Enable the Anthropic Claude models. Access is usually granted immediately.

Then confirm from the command line rather than trusting the console page:

```bash
aws bedrock list-foundation-models \
  --by-provider anthropic \
  --region us-east-1 \
  --query 'modelSummaries[].modelId' --output table
```

If the model this project uses is not in that list, check the inference profiles
too — several current models are only reachable through one:

```bash
aws bedrock list-inference-profiles \
  --region us-east-1 \
  --query 'inferenceProfileSummaries[].inferenceProfileId' --output table
```

Whichever id is actually available is the one to use. Pass it explicitly:

```bash
halo quote "..." --model us.anthropic.claude-sonnet-5
```

---

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
