"""The one place that talks to a model.

Everything else in the codebase takes a `ModelClient`, so tests substitute a fake
and never touch the network, and the cost of a run is counted in exactly one
place rather than estimated after the fact.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from halo.platform.budget import BudgetTracker

DEFAULT_MODEL = "anthropic.claude-sonnet-5"
"""Bedrock model ids carry an `anthropic.` prefix; the bare id is the first-party one."""

DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Dollars per million tokens, used to enforce the run budget — not to bill anyone.
# Bedrock is partner-operated and prices separately from the first-party API, so
# treat these as estimates and check them against the Bedrock pricing page before
# reading anything into a cost report.
PRICE_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "anthropic.claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "anthropic.claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "anthropic.claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}


def estimate_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Cost of one call. Unknown models cost zero rather than raising — a budget
    is a safety rail, and failing a run over an unrecognised price would make it
    a hazard instead."""
    if model not in PRICE_PER_MTOK:
        return Decimal("0.00")
    price_in, price_out = PRICE_PER_MTOK[model]
    million = Decimal(1_000_000)
    cost = (Decimal(input_tokens) / million) * price_in + (
        Decimal(output_tokens) / million
    ) * price_out
    return cost.quantize(Decimal("0.000001"))


@dataclass(frozen=True)
class ModelResult[T: BaseModel]:
    """A parsed model response plus what it cost to get it."""

    parsed: T
    input_tokens: int
    output_tokens: int
    usd: Decimal
    model: str
    stop_reason: str | None


class ModelClient(Protocol):
    """The seam every agent depends on. Implemented for real by `BedrockClient`
    and by a fake in the tests."""

    def parse[T: BaseModel](
        self,
        *,
        system: str,
        user: str,
        output_format: type[T],
        max_tokens: int = ...,
    ) -> ModelResult[T]: ...


class BedrockClient:
    """Claude on Amazon Bedrock, returning validated Pydantic objects.

    Structured output is used rather than "reply with JSON" prompting because the
    response is parsed straight into a domain model — a malformed answer becomes a
    validation error at the boundary instead of a `KeyError` three layers in.
    """

    def __init__(
        self,
        *,
        region: str = DEFAULT_REGION,
        model: str = DEFAULT_MODEL,
        tracker: BudgetTracker | None = None,
    ) -> None:
        from anthropic import AnthropicBedrockMantle

        self._client = AnthropicBedrockMantle(aws_region=region)
        self.model = model
        self.region = region
        self._tracker = tracker

    def parse[T: BaseModel](
        self,
        *,
        system: str,
        user: str,
        output_format: type[T],
        max_tokens: int = 16_000,
    ) -> ModelResult[T]:
        if self._tracker is not None:
            self._tracker.check()

        response = self._client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=output_format,
            thinking={"type": "adaptive"},
        )

        usage = response.usage
        usd = estimate_usd(self.model, usage.input_tokens, usage.output_tokens)
        if self._tracker is not None:
            self._tracker.record_model_call(usage.input_tokens, usage.output_tokens, usd)

        return ModelResult(
            parsed=response.parsed_output,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            usd=usd,
            model=self.model,
            stop_reason=response.stop_reason,
        )
