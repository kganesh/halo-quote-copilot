"""The only module that calls a model.

Everything else takes a `ModelClient`. Tests pass in a fake, so the test suite
never makes a network call. The cost of a run is counted here, in one place,
instead of being estimated afterwards.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel

from halo.platform.budget import BudgetTracker

DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-6"
"""The newest Sonnet this account can invoke, using the cheaper routing.

We use `global.` instead of `us.`. The Bedrock rate card prices the global
profile at 3.00/15.00 per million tokens. The regional profile costs 3.30/16.50
for the same model. The difference is that a global request is served wherever
capacity is available, instead of being restricted to one geography. That does
not matter for synthetic practice data. It would matter for real customer data.

Sonnet 5 is the model this project was planned around, and the account is
*authorized* for it. But Bedrock refuses the call with "not available for this
account", in every region tried, and after the Anthropic use case form cleared.
That is a tier the account is not offered, so 4.6 it is until that changes.

Bedrock has two API surfaces. They accept different model id formats. An account
can be entitled to one and not the other:

- **Mantle** (the Messages API on Bedrock, preferred for new code) takes bare ids
  like `anthropic.claude-sonnet-5`.
- **InvokeModel** (the older bedrock-runtime path) takes a cross-region inference
  profile id like `us.anthropic.claude-sonnet-4-6`, or a dated foundation-model id.

`BedrockClient` chooses the surface from the id format. Switching between them
requires only a `--model` change.
"""

PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")

DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Dollars per million tokens. These come from the Bedrock offer rate cards for
# us-east-1, read with `list-foundation-model-agreement-offers` under
# `usageBasedPricingTerm`. They are not the first-party Anthropic prices. The two
# are different. The earlier first-party figures under-reported this project's
# spend by about 10%.
#
# Each model has two rates, and the model id format selects one:
#   `us.` and other regional profiles pay the higher "Geo" rate.
#   `global.` profiles pay 10% less. In exchange, the request is served wherever
#   capacity is available instead of being restricted to one geography.
PRICE_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-sonnet-4-6": (Decimal("3.30"), Decimal("16.50")),
    "claude-opus-4-5": (Decimal("16.50"), Decimal("82.50")),
    "claude-sonnet-4-5": (Decimal("3.30"), Decimal("16.50")),
    "claude-haiku-4-5": (Decimal("1.10"), Decimal("5.50")),
}

GLOBAL_DISCOUNT = Decimal("0.909091")
"""`global.` profiles bill 3.00/15.00 where regional bills 3.30/16.50."""

# Cache rates are multiples of whatever the input rate is, and the same
# multiples apply to regional and global profiles. Read from the same rate card:
# on Sonnet 4.6 regional, input is 3.30 and cache read is 0.33; on global, input
# is 3.00 and cache read is 0.30. Both are a tenth. So the global discount is
# applied to the base rate first, and the multiplier after.
CACHE_READ_MULTIPLIER = Decimal("0.10")
"""A cached input token costs a tenth of a fresh one."""

CACHE_WRITE_5M_MULTIPLIER = Decimal("1.25")
"""Writing to the 5-minute cache costs a quarter more than a fresh input token.

Caching only pays off if the prefix is read back. One write plus one read costs
1.35x a single uncached call; the second read is where it turns profitable."""

CACHE_WRITE_1H_MULTIPLIER = Decimal("2.00")
"""The 1-hour cache costs twice a fresh input token to write."""


def _price_key(model: str) -> str | None:
    """Reduce any Bedrock id format to the model family used as the price key.

    `us.anthropic.claude-sonnet-4-6` and
    `anthropic.claude-sonnet-4-6-20260101-v1:0` are the same model at the same
    price. If the price table used the full id as its key, one of these would be
    priced at zero without any error.
    """
    trimmed = model
    for prefix in PROFILE_PREFIXES:
        trimmed = trimmed.removeprefix(prefix)
    trimmed = trimmed.removeprefix("anthropic.")
    return next((key for key in PRICE_PER_MTOK if trimmed.startswith(key)), None)


@dataclass(frozen=True)
class TokenCounts:
    """The four billable token categories in one response.

    They are separate counts, not subsets. `input_tokens` from the API excludes
    anything served from cache or written to it, so the real input size is the
    sum of all three input categories. Reading only `input_tokens` on a cached
    call under-reports both the spend and the context size.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def cache_write_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


def counts_from(usage: Any) -> TokenCounts:
    """Read the token categories out of an SDK usage object.

    The 5-minute and 1-hour caches are priced differently, so the breakdown in
    `cache_creation` is used when the SDK provides it. When it does not, the
    total falls back to the 5-minute rate, which is the default TTL and the
    cheaper of the two — so a missing breakdown under-reports rather than
    over-reports, and the run is not stopped by a budget it did not spend.
    """
    write_5m = write_1h = 0
    breakdown = getattr(usage, "cache_creation", None)
    if breakdown is not None:
        write_5m = getattr(breakdown, "ephemeral_5m_input_tokens", 0) or 0
        write_1h = getattr(breakdown, "ephemeral_1h_input_tokens", 0) or 0
    if not (write_5m or write_1h):
        write_5m = getattr(usage, "cache_creation_input_tokens", 0) or 0

    return TokenCounts(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


def estimate_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> Decimal:
    """Cost of one call.

    An unknown model returns zero instead of raising an exception. A budget is a
    safety limit. If an unrecognised price stopped the run, the safety limit
    would become the thing that breaks the system.
    """
    key = _price_key(model)
    if key is None:
        return Decimal("0.00")

    price_in, price_out = PRICE_PER_MTOK[key]
    if model.startswith("global."):
        price_in *= GLOBAL_DISCOUNT
        price_out *= GLOBAL_DISCOUNT

    million = Decimal(1_000_000)
    cost = (
        Decimal(input_tokens) * price_in
        + Decimal(output_tokens) * price_out
        + Decimal(cache_read_tokens) * price_in * CACHE_READ_MULTIPLIER
        + Decimal(cache_write_5m_tokens) * price_in * CACHE_WRITE_5M_MULTIPLIER
        + Decimal(cache_write_1h_tokens) * price_in * CACHE_WRITE_1H_MULTIPLIER
    ) / million
    return cost.quantize(Decimal("0.000001"))


def estimate_usd_for(model: str, counts: TokenCounts) -> Decimal:
    """`estimate_usd` for a whole `TokenCounts`."""
    return estimate_usd(
        model,
        counts.input_tokens,
        counts.output_tokens,
        cache_read_tokens=counts.cache_read_tokens,
        cache_write_5m_tokens=counts.cache_write_5m_tokens,
        cache_write_1h_tokens=counts.cache_write_1h_tokens,
    )


@dataclass(frozen=True)
class ModelResult[T: BaseModel]:
    """A parsed model response and what the call cost."""

    parsed: T
    input_tokens: int
    output_tokens: int
    usd: Decimal
    model: str
    stop_reason: str | None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class ModelTurn:
    """One assistant turn in a tool-use loop.

    `content` keeps the raw block list. That list must go back into the next
    request unchanged. Rebuilding it from extracted text loses the `tool_use`
    ids.
    """

    content: list[Any]
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    usd: Decimal
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def tool_uses(self) -> list[Any]:
        return [block for block in self.content if getattr(block, "type", None) == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(
            block.text for block in self.content if getattr(block, "type", None) == "text"
        )


def system_blocks(system: str) -> list[dict[str, Any]]:
    """The system prompt as one cacheable block.

    M6 runs four loops per request, and each loop resends its system prompt on
    every turn. That prefix is identical across turns and across specialists of
    the same kind, which is exactly the shape prompt caching is for: a cached
    input token costs a tenth of a fresh one.

    The breakpoint goes here and not on the messages, because the messages grow
    every turn and a breakpoint inside them would be invalidated by the growth.
    Anything volatile — today's date, the brief — stays after it.

    Two honest caveats. The minimum cacheable prefix is model-dependent and these
    prompts are shorter than it today, so `cache_read_input_tokens` will stay at
    zero until they grow; the accounting for it exists either way, and
    `Usage.cache_hit_rate` is the number to watch. And nothing in the offline
    suite can prove Bedrock accepted the breakpoint: the tests assert the shape
    of the request, and only a live run shows a cache read.
    """
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


class ModelClient(Protocol):
    """The interface every agent depends on.

    `BedrockClient` implements it for real. The tests implement it with a fake.
    """

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

    This uses structured output instead of asking the model to reply with JSON.
    The response is parsed directly into a domain model. A malformed answer
    becomes a validation error at the boundary, instead of a `KeyError` three
    layers deeper in the code.
    """

    def __init__(
        self,
        *,
        region: str = DEFAULT_REGION,
        model: str = DEFAULT_MODEL,
        tracker: BudgetTracker | None = None,
    ) -> None:
        from anthropic import AnthropicBedrock, AnthropicBedrockMantle

        # An inference-profile id works only with InvokeModel. A bare id works
        # only with Mantle. Choosing the client from the id format keeps the two
        # surfaces one flag apart, instead of two separate code paths.
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
            system=system_blocks(system),
            messages=[{"role": "user", "content": user}],
            output_format=output_format,
            thinking={"type": "adaptive"},
        )

        counts = counts_from(response.usage)
        usd = estimate_usd_for(self.model, counts)
        if self._tracker is not None:
            self._tracker.record_model_call(
                counts.input_tokens,
                counts.output_tokens,
                usd,
                cache_read_tokens=counts.cache_read_tokens,
                cache_write_tokens=counts.cache_write_tokens,
            )

        return ModelResult(
            parsed=response.parsed_output,
            input_tokens=counts.input_tokens,
            output_tokens=counts.output_tokens,
            usd=usd,
            model=self.model,
            stop_reason=response.stop_reason,
            cache_read_tokens=counts.cache_read_tokens,
            cache_write_tokens=counts.cache_write_tokens,
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

        This is deliberately not a `while stop_reason == "tool_use"` helper. The
        loop is where budgets are checked and tool results are audited. Putting
        the loop here would move both of those outside the agent's control.
        """
        if self._tracker is not None:
            self._tracker.check()

        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_blocks(system),
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
        )

        counts = counts_from(response.usage)
        usd = estimate_usd_for(self.model, counts)
        if self._tracker is not None:
            self._tracker.record_model_call(
                counts.input_tokens,
                counts.output_tokens,
                usd,
                cache_read_tokens=counts.cache_read_tokens,
                cache_write_tokens=counts.cache_write_tokens,
            )

        return ModelTurn(
            content=list(response.content),
            stop_reason=response.stop_reason,
            input_tokens=counts.input_tokens,
            output_tokens=counts.output_tokens,
            usd=usd,
            cache_read_tokens=counts.cache_read_tokens,
            cache_write_tokens=counts.cache_write_tokens,
        )
