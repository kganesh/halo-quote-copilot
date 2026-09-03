"""`halo doctor` — a preflight for the AWS setup M1 needs.

Checks run in dependency order and stop at the first failure, so the output is
one thing to fix rather than a wall of red. Every failure names the step in
`docs/aws-setup.md` that fixes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import anthropic
from pydantic import BaseModel


class Ping(BaseModel):
    """The smallest schema worth asking a model to fill in."""

    ok: bool


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str
    fix: str | None = None


def check_credentials() -> Check:
    """Does anything in the AWS chain resolve, and to whom?"""
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover - boto3 ships with anthropic[bedrock]
        return Check("credentials", False, "boto3 is not installed", "pip install -e .")

    try:
        identity = boto3.client("sts").get_caller_identity()
    except (BotoCoreError, ClientError) as exc:
        return Check(
            "credentials",
            False,
            str(exc).split("\n")[0],
            "aws-setup.md step 1 — `aws configure`, or export AWS_PROFILE",
        )
    return Check("credentials", True, identity.get("Arn", "resolved"))


def check_region(region: str) -> Check:
    if not region:
        return Check("region", False, "no region set", "aws-setup.md step 2 — export AWS_REGION")
    return Check("region", True, region)


def check_model_available(region: str, model: str) -> Check:
    """Whether the account is *entitled* to this model, not merely whether
    Bedrock offers it.

    `ListFoundationModels` returns the region's whole catalogue regardless of
    what the account may call, so membership there reports a pass for a model
    that 403s on the very next line. `GetFoundationModelAvailability` answers the
    question actually being asked.

    A failure to *query* is still a pass with a caveat: a missing listing
    permission is not a missing model, and the invoke check settles it either way.
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover
        return Check("model access", True, "not checked (boto3 missing)")

    from halo.platform.bedrock import PROFILE_PREFIXES

    bare = model
    for prefix in PROFILE_PREFIXES:
        bare = bare.removeprefix(prefix)

    try:
        client = boto3.client("bedrock", region_name=region)
        catalogue = {m["modelId"] for m in client.list_foundation_models()["modelSummaries"]}
    except (BotoCoreError, ClientError) as exc:
        return Check("entitlement", True, f"not checked ({str(exc).split(chr(10))[0][:60]})")

    match = next((entry for entry in sorted(catalogue) if entry.startswith(bare)), None)
    if match is None:
        offered = sorted(m for m in catalogue if m.startswith("anthropic."))[-3:]
        return Check(
            "entitlement",
            False,
            f"{model} is not in the {region} catalogue. Newest offered: "
            f"{', '.join(offered) or 'none'}",
            "aws-setup.md step 4 — pass --model with an id Bedrock lists",
        )

    try:
        availability = client.get_foundation_model_availability(modelId=match)
    except (BotoCoreError, ClientError) as exc:
        return Check("entitlement", True, f"not checked ({str(exc).split(chr(10))[0][:60]})")

    authorized = availability.get("authorizationStatus")
    entitled = availability.get("entitlementAvailability")
    if authorized == "AUTHORIZED" and entitled == "AVAILABLE":
        return Check("entitlement", True, f"{match} authorized in {region}")

    return Check(
        "entitlement",
        False,
        f"{match}: authorization={authorized}, entitlement={entitled}",
        "aws-setup.md step 4 — request access under Model access in the Bedrock console",
    )


def check_invoke(region: str, model: str) -> Check:
    """The only check that proves anything: one real, tiny call."""
    from halo.platform.bedrock import BedrockClient

    try:
        client = BedrockClient(region=region, model=model)
        result = client.parse(
            system="Reply with ok true.",
            user="ping",
            output_format=Ping,
            max_tokens=256,
        )
    except anthropic.APIStatusError as exc:
        return Check("invoke", False, _bedrock_message(exc), _bedrock_fix(exc))
    except (anthropic.APIError, RuntimeError) as exc:
        return Check("invoke", False, f"{type(exc).__name__}: {str(exc)[:160]}", "aws-setup.md")

    return Check(
        "invoke",
        True,
        f"{result.input_tokens} in + {result.output_tokens} out tokens, "
        f"${result.usd:.6f} estimated",
    )


def _bedrock_message(exc: anthropic.APIStatusError) -> str:
    """Bedrock puts the useful sentence inside the body; str(exc) buries it."""
    body = exc.body if isinstance(exc.body, dict) else {}
    nested = body.get("error", {}) if isinstance(body.get("error"), dict) else {}
    message = body.get("message") or nested.get("message") or str(exc)
    return f"{exc.status_code}: {message[:180]}"


def _bedrock_fix(exc: anthropic.APIStatusError) -> str:
    """Three refusals read alike and are fixed in three different places."""
    text = _bedrock_message(exc).lower()
    if "use case details" in text:
        return (
            "Bedrock console -> Model access -> submit the Anthropic use case details "
            "form. It gates every Anthropic model on the account, whatever the id."
        )
    if "not available for this account" in text:
        return (
            "This model tier is not offered to the account. Use one the entitlement "
            "check passes for, or follow the access route in the message."
        )
    if exc.status_code == 403:
        return "aws-setup.md step 3 — IAM policy for bedrock:InvokeModel"
    return "aws-setup.md step 4 — model access, or an inference-profile id"


def run(region: str, model: str) -> tuple[list[Check], bool]:
    """Run the checks in dependency order, stopping at the first failure."""
    checks: list[Check] = []
    for check in (
        lambda: check_credentials(),
        lambda: check_region(region),
        lambda: check_model_available(region, model),
        lambda: check_invoke(region, model),
    ):
        result = check()
        checks.append(result)
        if not result.passed:
            return checks, False
    return checks, True


def render(checks: list[Check], ok: bool, *, region: str, model: str) -> str:
    lines = [f"model {model}   region {region}", ""]
    width = max(len(c.name) for c in checks)
    for check in checks:
        mark = "PASS" if check.passed else "FAIL"
        lines.append(f"  [{mark}] {check.name.ljust(width)}  {check.detail}")
        if check.fix:
            lines.append(f"         {' ' * width}  -> {check.fix}")
    lines.append("")
    lines.append(
        'Ready. Try: halo quote "500 hoodies, 3-colour front print, Chicago by Oct 15"'
        if ok
        else f"Stopped at the first problem. See docs/aws-setup.md ({len(checks)} of 4 checks run)."
    )
    return "\n".join(lines)


def estimate_note() -> str:
    return (
        "Cost shown is an estimate from first-party rates; Bedrock prices "
        f"separately. One M1 draft is roughly ${Decimal('0.011'):.3f}."
    )
