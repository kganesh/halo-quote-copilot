"""A model client that returns a canned object, so the suite never bills anyone."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from halo.domain.catalog import DecorationMethod
from halo.domain.request import QuoteRequest, UngroundedDraft, UngroundedLine
from halo.platform.bedrock import ModelResult, estimate_usd
from halo.platform.budget import BudgetTracker


def a_draft(**overrides) -> UngroundedDraft:
    return UngroundedDraft(
        **{
            "request": QuoteRequest(
                product_description="mid-weight fleece hoodies",
                quantity=500,
                decoration_method=DecorationMethod.SCREEN_PRINT,
                imprint_colors=3,
                imprint_location="front",
                ship_to_city="Chicago",
                ship_to_state="IL",
                needed_by=date(2026, 10, 15),
                budget_usd=Decimal("12000.00"),
            ),
            "lines": [
                UngroundedLine(
                    sku="HL-KNT-2200",
                    description="Mid-weight fleece hoodie, navy",
                    quantity=500,
                    unit_price=Decimal("15.80"),
                )
            ],
            "decoration_setup_fee": Decimal("66.00"),
            "decoration_run_charge_per_unit": Decimal("1.55"),
            "shipping_cost": Decimal("310.00"),
            "promised_ship_date": date(2026, 10, 6),
            "assumptions": [
                "assumed $15.80 unit price for a mid-weight fleece hoodie at 500 units",
                "assumed $22.00 per screen setup for a 3-colour front print",
                "assumed 7 business days decoration lead time",
                "assumed zone 3 ground freight from an unnamed decorator",
            ],
            **overrides,
        }
    )


class FakeModelClient:
    """Records what it was asked and returns what it was told to.

    `tracker` is optional so a test can assert the agent's behaviour with and
    without budget accounting attached.
    """

    def __init__(
        self,
        parsed: BaseModel | None = None,
        *,
        tracker: BudgetTracker | None = None,
        input_tokens: int = 1_450,
        output_tokens: int = 820,
        model: str = "us.anthropic.claude-sonnet-4-6",
    ) -> None:
        self.parsed = parsed if parsed is not None else a_draft()
        self.calls: list[dict] = []
        self._tracker = tracker
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.model = model

    def parse(self, *, system, user, output_format, max_tokens=16_000):
        if self._tracker is not None:
            self._tracker.check()
        self.calls.append(
            {
                "system": system,
                "user": user,
                "output_format": output_format,
                "max_tokens": max_tokens,
            }
        )
        usd = estimate_usd(self.model, self._input_tokens, self._output_tokens)
        if self._tracker is not None:
            self._tracker.record_model_call(self._input_tokens, self._output_tokens, usd)
        return ModelResult(
            parsed=self.parsed,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            usd=usd,
            model=self.model,
            stop_reason="end_turn",
        )
