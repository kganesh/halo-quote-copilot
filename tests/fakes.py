"""A model client that returns a fixed object, so the test suite never spends
money."""

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
    """Records the calls it receives and returns a fixed response.

    `tracker` is optional, so a test can check the agent's behaviour both with
    and without budget accounting.
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


class Block:
    """A content block shaped like the SDK's."""

    def __init__(self, type: str, **fields) -> None:
        self.type = type
        self.__dict__.update(fields)


class SpecialistModel:
    """Drives M6's specialists: calls each one's tools, then reports what came back.

    It identifies which specialist is asking from the tool list it was given,
    which is unambiguous and does not depend on prompt wording. Reports are built
    from the gateway's audit, so every figure it cites is a figure a tool really
    returned — the provenance check has to pass for the right reason.
    """

    model = "fake.specialist"

    def __init__(
        self,
        gateway,
        *,
        sku: str,
        quantity: int = 500,
        state: str = "IL",
        method: str = "screen_print",
        colors: int = 3,
        description: str = "branded merchandise",
        today=None,
    ) -> None:
        from datetime import date

        self._gateway = gateway
        self._sku = sku
        self._description = description
        self._quantity = quantity
        self._state = state
        self._method = method
        self._colors = colors
        self._today = today or date.today()
        self._turns: dict[str, int] = {}
        self.parsed: list[str] = []

    def _plan(self, tools) -> list[tuple[str, callable]]:
        names = {tool["name"] for tool in tools}
        if "get_price" in names:
            return [
                ("search_products", lambda: {"description": self._description, "limit": 3}),
                ("get_price", lambda: {"sku": self._sku, "quantity": self._quantity}),
                ("get_margin_policy", lambda: {"category": self._category()}),
            ]
        if "earliest_ship_date" in names:
            return [
                (
                    "check_inventory",
                    lambda: {
                        "sku": self._sku,
                        "quantity": self._quantity,
                        "method": self._method,
                    },
                ),
                (
                    "get_decoration_charges",
                    lambda: {
                        "method": self._method,
                        "colors": self._colors,
                        "units": self._quantity,
                    },
                ),
                (
                    "earliest_ship_date",
                    lambda: {
                        "supplier_id": self._supplier(),
                        "method": self._method,
                        "units": self._quantity,
                        "from_day": self._today.isoformat(),
                    },
                ),
            ]
        return [("estimate_freight", lambda: {"to_state": self._state, "units": self._quantity})]

    def _category(self) -> str:
        from halo.mcp_servers.store import products

        return next(p["category"] for p in products() if p["sku"] == self._sku)

    def _name(self) -> str:
        from halo.mcp_servers.store import products

        return next(p["name"] for p in products() if p["sku"] == self._sku)

    def _supplier(self) -> str:
        rows = self._result("supplier.check_inventory")
        return rows[0]["supplier_id"]

    def _result(self, route: str):
        return next(c.result for c in self._gateway.audit if c.name == route and c.ok)

    def _call_id(self, route: str) -> str:
        return next(c.id for c in self._gateway.audit if c.name == route and c.ok)

    def converse(self, *, system, messages, tools, max_tokens=8_000):
        from decimal import Decimal

        from halo.platform.bedrock import ModelTurn

        key = ",".join(sorted(tool["name"] for tool in tools))
        step = self._turns.get(key, 0)
        self._turns[key] = step + 1
        plan = self._plan(tools)

        if step >= len(plan):
            return ModelTurn(
                content=[Block("text", text="Done.")],
                stop_reason="end_turn",
                input_tokens=200,
                output_tokens=60,
                usd=Decimal("0"),
            )
        name, arguments = plan[step]
        return ModelTurn(
            content=[Block("tool_use", id=f"tu-{step}", name=name, input=arguments())],
            stop_reason="tool_use",
            input_tokens=200,
            output_tokens=60,
            usd=Decimal("0"),
        )

    def parse(self, *, system, user, output_format, max_tokens=16_000):
        from datetime import date
        from decimal import Decimal

        from halo.agents.provenance import SourcedFigure
        from halo.agents.specialists import LogisticsReport, PricingReport, SupplyReport
        from halo.platform.bedrock import ModelResult

        self.parsed.append(output_format.__name__)

        if output_format is PricingReport:
            price = self._result("pim_oms.get_price")
            policy = self._result("pim_oms.get_margin_policy")
            product = (
                next(p for p in self._result("pim_oms.search_products") if p["sku"] == self._sku)
                if any(p["sku"] == self._sku for p in self._result("pim_oms.search_products"))
                else {"name": "Meridian Ring-Spun Tee"}
            )
            parsed = PricingReport(
                sku=self._sku,
                product_name=product["name"],
                category=self._category(),
                quantity=self._quantity,
                unit_price=SourcedFigure(
                    value=Decimal(price["unit_price"]),
                    tool_call_id=self._call_id("pim_oms.get_price"),
                ),
                base_cost=SourcedFigure(
                    value=Decimal(price["base_cost"]),
                    tool_call_id=self._call_id("pim_oms.get_price"),
                ),
                margin_floor_pct=SourcedFigure(
                    value=Decimal(policy["floor_pct"]),
                    tool_call_id=self._call_id("pim_oms.get_margin_policy"),
                ),
            )
        elif output_format is SupplyReport:
            charges = self._result("supplier.get_decoration_charges")
            ship = self._result("supplier.earliest_ship_date")
            parsed = SupplyReport(
                supplier_id=ship["supplier_id"],
                supplier_name=ship["supplier_name"],
                decoration_setup_fee=SourcedFigure(
                    value=Decimal(charges["setup_fee"]),
                    tool_call_id=self._call_id("supplier.get_decoration_charges"),
                ),
                decoration_run_charge_per_unit=SourcedFigure(
                    value=Decimal(charges["run_charge_per_unit"]),
                    tool_call_id=self._call_id("supplier.get_decoration_charges"),
                ),
                promised_ship_date=date.fromisoformat(ship["ship_date"]),
                ship_date_tool_call_id=self._call_id("supplier.earliest_ship_date"),
            )
        elif output_format is LogisticsReport:
            freight = self._result("shipping.estimate_freight")
            call_id = self._call_id("shipping.estimate_freight")
            parsed = LogisticsReport(
                freight_cost=SourcedFigure(
                    value=Decimal(freight["freight_cost"]), tool_call_id=call_id
                ),
                transit_days=SourcedFigure(
                    value=Decimal(freight["transit_days"]), tool_call_id=call_id
                ),
            )
        else:
            raise AssertionError(f"no script for {output_format.__name__}")

        return ModelResult(
            parsed=parsed,
            input_tokens=400,
            output_tokens=120,
            usd=Decimal("0"),
            model=self.model,
            stop_reason="end_turn",
        )
