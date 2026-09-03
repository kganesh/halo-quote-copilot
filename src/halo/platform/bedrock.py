"""The one place that talks to a model.

Everything else in the codebase takes a `ModelClient`, so tests substitute a fake
and never touch the network, and the cost of a run is counted in exactly one
place rather than estimated after the fact.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel

from halo.platform.budget import BudgetTracker

DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-6"
"""The newest Sonnet this account can invoke, on the cheaper routing.

`global.` rather than `us.`: the Bedrock rate card prices the global profile at
3.00/15.00 against the regional 3.30/16.50, for the same model. The trade is that
a global request is served wherever there is capacity rather than pinned to a
geography — irrelevant for synthetic practice data, worth a second thought for
anything real.

Sonnet 5 is the model this project was planned around, and the account is
*authorized* for it — but Bedrock refuses the call with "not available for this
account", in every region tried, and after the Anthropic use case form cleared.
That is a tier the account is not offered, so 4.6 it is until that changes.

Two different Bedrock surfaces accept two different id shapes, and an account may
be entitled to one and not the other:

- **Mantle** (the Messages API on Bedrock, preferred for new code) takes bare ids
  like `anthropic.claude-sonnet-5`.
- **InvokeModel** (the older bedrock-runtime path) takes a cross-region inference
  profile id like `us.anthropic.claude-sonnet-4-6`, or a dated foundation-model id.

`BedrockClient` picks the surface from the id shape, so switching between them is
a `--model` change and nothing else.
"""

PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")

DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Dollars per million tokens, read from the Bedrock offer rate cards for
# us-east-1 (`list-foundation-model-agreement-offers` -> usageBasedPricingTerm),
# not from the first-party price list. The two differ, and the earlier
# first-party figures under-reported this project's spend by about 10%.
#
# Two rates per model, because the id shape picks one:
#   `us.` / regional profiles pay the higher "Geo" rate.
#   `global.` profiles pay 10% less, in exchange for the request being served
#   wherever there is capacity rather than pinned to a geography.
PRICE_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-sonnet-4-6": (Decimal("3.30"), Decimal("16.50")),
    "claude-opus-4-5": (Decimal("16.50"), Decimal("82.50")),
    "claude-sonnet-4-5": (Decimal("3.30"), Decimal("16.50")),
    "claude-haiku-4-5": (Decimal("1.10"), Decimal("5.50")),
}

GLOBAL_DISCOUNT = Decimal("0.909091")
"""`global.` profiles bill 3.00/15.00 where regional bills 3.30/16.50."""

CACHE_READ_FRACTION = Decimal("0.1")
"""A cached input token costs a tenth of a fresh one — $0.33 against $3.30 on
Sonnet 4.6. The sourcing loop resends its whole transcript every turn, so this is
the lever to pull when a tool loop gets expensive."""


def _price_key(model: str) -> str | None:
    """Reduce any Bedrock id shape to the model family the price table keys on.

    `us.anthropic.claude-sonnet-4-6` and `anthropic.claude-sonnet-4-6-20260101-v1:0`
    are the same model at the same price; keying on the full id would silently
    price one of them at zero.
    """
    trimmed = model
    for prefix in PROFILE_PREFIXES:
        trimmed = trimmed.removeprefix(prefix)
    trimmed = trimmed.removeprefix("anthropic.")
    return next((key for key in PRICE_PER_MTOK if trimmed.startswith(key)), None)


def estimate_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Cost of one call. Unknown models cost zero rather than raising — a budget
    is a safety rail, and failing a run over an unrecognised price would make it
    a hazard instead."""
    key = _price_key(model)
    if key is None:
        return Decimal("0.00")
    price_in, price_out = PRICE_PER_MTOK[key]
    if model.startswith("global."):
        price_in *= GLOBAL_DISCOUNT
        price_out *= GLOBAL_DISCOUNT
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


@dataclass(frozen=True)
class ModelTurn:
    """One assistant turn in a tool-use loop.

    `content` is kept as the raw block list because it has to go back into the
    next request unchanged — rebuilding it from extracted text is how tool_use
    ids get lost.
    """

    content: list[Any]
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    usd: Decimal

    @property
    def tool_uses(self) -> list[Any]:
        return [block for block in self.content if getattr(block, "type", None) == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(
            block.text for block in self.content if getattr(block, "type", None) == "text"
        )


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

    def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = ...,
    ) -> ModelTurn: ...


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
        from anthropic import AnthropicBedrock, AnthropicBedrockMantle

        # An inference-profile id only means something to InvokeModel; a bare id
        # only means something to Mantle. Choosing from the id keeps the two
        # surfaces one flag apart instead of two code paths.
        uses_profile = model.startswith(PROFILE_PREFIXES)
        self._client = (
            AnthropicBedrock(aws_region=region)
            if uses_profile
            else AnthropicBedrockMantle(aws_region=region)
        )
        self.surface = "invoke_model" if uses_profile else "mantle"
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

    def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 8_000,
    ) -> ModelTurn:
        """One turn of a tool-use loop. The caller owns the loop.

        Deliberately not a `while stop_reason == "tool_use"` helper: the loop is
        where budgets are checked and tool results are audited, and burying it
        here would put both outside the agent's control.
        """
        if self._tracker is not None:
            self._tracker.check()

        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
        )

        usage = response.usage
        usd = estimate_usd(self.model, usage.input_tokens, usage.output_tokens)
        if self._tracker is not None:
            self._tracker.record_model_call(usage.input_tokens, usage.output_tokens, usd)

        return ModelTurn(
            content=list(response.content),
            stop_reason=response.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            usd=usd,
        )
