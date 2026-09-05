# The stack

```bash
terraform init
terraform apply
eval "$(terraform output -json environment | jq -r 'to_entries[] | "export \(.key)=\(.value)"')"
```

That is the whole thing: five modules, no VPC, and an output block that hands you
the environment variables `halo` reads.

```bash
terraform destroy
halo teardown --live      # the morning after, when Cost Explorer has caught up
```

## What is here

| module | why it exists |
|---|---|
| `identity` | Cognito user pool, client and one group per role. `platform/admission.py` maps its claims. |
| `guardrail` | The Bedrock guardrail M4 applies to both surfaces, published to a version. |
| `evidence` | The M7 event bucket, partitioned, encrypted, expiring. |
| `observability` | One log group, with retention set in the same resource that creates it. |
| `access` | Exactly what the CLI needs: invoke, guard, write evidence, read own spend. |

## What is deliberately absent

No VPC, and therefore no NAT gateway — this application talks to Bedrock,
Cognito and S3, all public endpoints, so a VPC would exist to hold nothing while
billing about $32 a month for the privilege.

No database: Atlas is 25 documents in SQLite beside the CLI, and Aurora
Serverless v2 has a minimum capacity that bills per ACU-hour.

No container runtime: `halo` is a CLI, not a service, and anything kept warm for
latency bills continuously.

Each of those is a module this stack grows when there is something to put in it.
`src/halo/infra.py` holds the list of resource types that bill while idle, so
adding one is a decision someone makes on purpose rather than a dependency that
arrives with a copied module.

## Why local state

A remote backend is an S3 bucket and a lock table that outlive `terraform
destroy`, which is the exact thing this milestone exists to disprove. A team
needs one. An account being stood up and torn down needs a state file it can
delete.

## The teardown is checked, not asserted

`halo teardown` parses this configuration and fails on anything that would
survive a destroy: a bucket without `force_destroy`, a `prevent_destroy`
lifecycle, a log group with no retention, or any resource on the idle-billing
list. It runs in CI on every push, as part of `halo gate`.

`halo teardown --live` asks Cost Explorer what the account was charged over the
last two days. Two days because Cost Explorer lags: a destroy this morning is not
visible until tomorrow, and a report run immediately after would say zero and
mean nothing.
