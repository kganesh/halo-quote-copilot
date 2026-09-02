"""`halo doctor` — a preflight for the AWS setup M1 needs.

Checks run in dependency order and stop at the first failure, so the output is
one thing to fix rather than a wall of red. Every failure names the step in
`docs/aws-setup.md` that fixes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

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
    """Ask Bedrock what it actually has, rather than trusting the console page.

    A listing failure is reported as a pass with a caveat: the invoke check below
    is the real test, and a missing `ListFoundationModels` permission should not
    read as "the model is unavailable".
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover
        return Check("model access", True, "not checked (boto3 missing)")

    bare = model.split(".", 1)[1] if model.startswith(("us.", "eu.", "apac.")) else model
    try:
        client = boto3.client("bedrock", region_name=region)
        available = {m["modelId"] for m in client.list_foundation_models()["modelSummaries"]}
    except (BotoCoreError, ClientError) as exc:
        return Check("model access", True, f"not checked ({str(exc).split(chr(10))[0][:60]})")

    if any(entry.startswith(bare) for entry in available):
        return Check("model access", True, f"{model} listed in {region}")

    anthropic_models = sorted(m for m in available if m.startswith("anthropic."))
    hint = ", ".join(anthropic_models[:4]) or "none"
    return Check(
        "model access",
        False,
        f"{model} is not listed in {region}. Anthropic models present: {hint}",
        "aws-setup.md step 4 — enable it under Model access, or pass --model",
    )


def check_invoke(region: str, model: str) -> Check:
    """The only check that proves anything: one real, tiny call."""
    import anthropic

    from halo.platform.bedrock import BedrockClient

    try:
        client = BedrockClient(region=region, model=model)
        result = client.parse(
            system="Reply with ok true.",
            user="ping",
            output_format=Ping,
            max_tokens=256,
        )
    except anthropic.PermissionDeniedError as exc:
        return Check(
            "invoke",
            False,
            str(exc)[:120],
            "aws-setup.md step 3 — IAM policy for bedrock:InvokeModel",
        )
    except anthropic.NotFoundError as exc:
        return Check(
            "invoke", False, str(exc)[:120], "aws-setup.md step 4 — model access, or a profile id"
        )
    except (anthropic.APIError, RuntimeError) as exc:
        return Check("invoke", False, f"{type(exc).__name__}: {str(exc)[:100]}", "aws-setup.md")

    return Check(
        "invoke",
        True,
        f"{result.input_tokens} in + {result.output_tokens} out tokens, "
        f"${result.usd:.6f} estimated",
    )


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
