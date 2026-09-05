"""M8's done-when, as a check rather than a promise.

"`destroy` leaves no billable resource" is the kind of claim every practice
project makes and few verify. There are two halves to verifying it, and only one
of them needs an AWS account.

**Before applying** — read the configuration and refuse the ones that are known
not to tear down cleanly. A bucket with objects blocks `destroy` unless
`force_destroy` is set. A `prevent_destroy` lifecycle makes `destroy` fail by
design. A log group with no retention outlives the stack in the only way that
still bills. And a short list of resources bill while completely idle, so the
question for each is not "is it configured well" but "is it here at all".

**After destroying** — ask Cost Explorer. That is the only authority on whether
the account went back to zero, because the thing that bills after a teardown is
by definition the thing Terraform did not know about: a log group created by a
service on first use, an EBS snapshot, an Elastic IP someone allocated by hand
during the demo.

The first half runs in CI on every push. The second needs credentials and a day
of billing latency, so it is a command someone runs the morning after.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

TERRAFORM_DIR = Path(__file__).resolve().parents[2] / "infra" / "terraform"

IDLE_BILLERS = {
    "aws_nat_gateway": "~$32/month idle, plus data. Use VPC endpoints instead.",
    "aws_eip": "billed while allocated, whether or not it is attached.",
    "aws_rds_cluster": "Aurora bills a minimum capacity per ACU-hour.",
    "aws_db_instance": "an instance bills whether or not it is queried.",
    "aws_opensearchserverless_collection": "billed per OCU-hour with a floor.",
    "aws_elasticache_cluster": "bills per node-hour, always on.",
    "aws_ecs_service": "anything kept warm for latency bills continuously.",
    "aws_lb": "a load balancer bills per hour with no traffic.",
    "aws_msk_cluster": "brokers bill per hour.",
}
"""Resources that cost money doing nothing.

Taken from this project's own cost list rather than invented: each one is a
line item that shows up on a practice account and stays there. A stack that
needs one of these can add it here with a reason — the check exists to make that
a decision someone makes on purpose, not a dependency that arrived with a module
someone copied.
"""

RETENTION_REQUIRED = {
    "aws_cloudwatch_log_group": "retention_in_days",
}
"""Resources whose default is "keep forever", which is the expensive default."""


@dataclass(frozen=True)
class InfraFinding:
    resource: str
    problem: str

    def __str__(self) -> str:
        return f"{self.resource}: {self.problem}"


def load(directory: Path | None = None) -> list[tuple[str, str, dict[str, Any]]]:
    """Every resource in the stack, as (type, name, body).

    Parsed rather than grepped. A comment mentioning `aws_nat_gateway` should not
    fail a build, and a resource split across lines should not pass one.
    """
    import hcl2

    found: list[tuple[str, str, dict[str, Any]]] = []
    for path in sorted((directory or TERRAFORM_DIR).rglob("*.tf")):
        with path.open() as handle:
            parsed = hcl2.load(handle)
        for block in parsed.get("resource", []):
            for resource_type, bodies in block.items():
                for name, body in bodies.items():
                    # hcl2 keeps the quotes on block labels, so the type arrives
                    # as '"aws_s3_bucket"'. Left alone, every lookup below misses
                    # and the check passes by finding nothing — which is how a
                    # gate ends up reporting success for the wrong reason.
                    found.append((_label(resource_type), _label(name), body))
    return found


def _label(value: str) -> str:
    return value.strip('"')


def check(directory: Path | None = None) -> list[InfraFinding]:
    """Everything about this stack that would survive a `terraform destroy`."""
    findings: list[InfraFinding] = []

    for resource_type, name, body in load(directory):
        label = f"{resource_type}.{name}"

        if reason := IDLE_BILLERS.get(resource_type):
            findings.append(InfraFinding(label, f"bills while idle — {reason}"))

        lifecycle = body.get("lifecycle")
        blocks = lifecycle if isinstance(lifecycle, list) else [lifecycle] if lifecycle else []
        for block in blocks:
            if isinstance(block, dict) and block.get("prevent_destroy"):
                findings.append(
                    InfraFinding(label, "prevent_destroy makes `terraform destroy` fail by design")
                )

        if (field := RETENTION_REQUIRED.get(resource_type)) and not body.get(field):
            findings.append(
                InfraFinding(label, f"no {field}: this outlives the stack and bills per GB-month")
            )

        if resource_type == "aws_s3_bucket" and not body.get("force_destroy"):
            findings.append(
                InfraFinding(
                    label,
                    "no force_destroy: `destroy` fails once anything has been written, "
                    "and the teardown quietly becomes a manual step",
                )
            )

    return findings


def cost_since(client: Any, start: date, end: date) -> list[tuple[str, str]]:
    """Non-zero cost by service between two days, newest data first.

    Cost Explorer is the authority on a teardown because it sees what Terraform
    did not create: a log group a service made on first use, a snapshot, an
    address someone allocated by hand during the demo.
    """
    response = client.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    charged: dict[str, float] = {}
    for period in response.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount > 0:
                service = group["Keys"][0]
                charged[service] = charged.get(service, 0.0) + amount

    return sorted(
        ((service, f"{total:.4f}") for service, total in charged.items()),
        key=lambda r: -float(r[1]),
    )


def verify_teardown(client: Any = None, *, days: int = 2) -> list[tuple[str, str]]:
    """What the account has been charged since the teardown.

    Two days by default, because Cost Explorer lags: a `destroy` this morning is
    not visible until tomorrow, and a report run immediately after would always
    say zero and always be meaningless.
    """
    if client is None:
        import boto3

        client = boto3.client("ce")

    today = datetime.now(UTC).date()
    return cost_since(client, today - timedelta(days=days), today + timedelta(days=1))


def report(findings: list[InfraFinding]) -> str:
    if not findings:
        return "No resource in this stack survives `terraform destroy` or bills while idle."
    return "\n".join(f"  {finding}" for finding in findings)
