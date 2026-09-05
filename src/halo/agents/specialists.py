"""The four jobs the M2 loop was doing at once.

Pricing, supply, logistics and policy. The split is not organisational tidiness:
each of these has a different failure, a different budget and a different person
who fixes it when it breaks. Pricing gets a margin wrong; supply promises a date
the plant cannot hold; logistics quotes the wrong zone; policy invents an imprint
rule. Only the last one is a retrieval problem, and in M2 all four arrived as
"the quote is wrong".

Budgets are per specialist and are deliberately unequal. Pricing makes three
short calls and needs almost nothing. Supply searches a capacity calendar and
needs room. Policy reads six excerpts of prose and is the only one that pays for
a large input.

The policy specialist has no tools and is not defined here: it is the M3 advisor,
run with its own budget by the supervisor. Giving it a second implementation
would mean two verbatim-quote checks to keep in step.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from halo.agents.loop import Specialist
from halo.agents.provenance import FigureCheck, SourcedFigure
from halo.platform.budget import Budget

PRICING_BUDGET = Budget(
    wall_clock_seconds=90, max_tokens=40_000, max_tool_calls=8, max_usd=Decimal("0.25")
)
SUPPLY_BUDGET = Budget(
    wall_clock_seconds=120, max_tokens=60_000, max_tool_calls=10, max_usd=Decimal("0.35")
)
LOGISTICS_BUDGET = Budget(
    wall_clock_seconds=60, max_tokens=30_000, max_tool_calls=5, max_usd=Decimal("0.20")
)
POLICY_BUDGET = Budget(
    wall_clock_seconds=90, max_tokens=60_000, max_tool_calls=0, max_usd=Decimal("0.25")
)


class PricingReport(BaseModel):
    """What it costs us and what we sell it for, both sourced.

    `base_cost` is here because M6 needs a margin, and a margin computed from a
    cost the model remembered is not a margin. It is checked like every other
    figure.
    """

    sku: str
    product_name: str
    category: str
    quantity: int = Field(ge=1)
    unit_price: SourcedFigure
    base_cost: SourcedFigure
    margin_floor_pct: SourcedFigure
    notes: list[str] = Field(default_factory=list)

    def figure_checks(self) -> list[FigureCheck]:
        return [
            FigureCheck(
                "unit_price", self.unit_price.value, self.unit_price.tool_call_id, ("price",)
            ),
            FigureCheck("base_cost", self.base_cost.value, self.base_cost.tool_call_id, ("cost",)),
            FigureCheck(
                "margin_floor_pct",
                self.margin_floor_pct.value,
                self.margin_floor_pct.tool_call_id,
                ("floor",),
            ),
        ]


class SupplyReport(BaseModel):
    supplier_id: str
    supplier_name: str
    decoration_setup_fee: SourcedFigure
    decoration_run_charge_per_unit: SourcedFigure
    promised_ship_date: date
    ship_date_tool_call_id: str = Field(pattern=r"^tc-\d{4}$")
    notes: list[str] = Field(default_factory=list)

    def figure_checks(self) -> list[FigureCheck]:
        return [
            FigureCheck(
                "decoration_setup_fee",
                self.decoration_setup_fee.value,
                self.decoration_setup_fee.tool_call_id,
                ("setup",),
            ),
            FigureCheck(
                "decoration_run_charge_per_unit",
                self.decoration_run_charge_per_unit.value,
                self.decoration_run_charge_per_unit.tool_call_id,
                ("run_charge",),
            ),
            FigureCheck(
                "promised_ship_date",
                self.promised_ship_date,
                self.ship_date_tool_call_id,
                ("date",),
            ),
        ]


class LogisticsReport(BaseModel):
    freight_cost: SourcedFigure
    transit_days: SourcedFigure
    notes: list[str] = Field(default_factory=list)

    def figure_checks(self) -> list[FigureCheck]:
        return [
            FigureCheck(
                "freight_cost",
                self.freight_cost.value,
                self.freight_cost.tool_call_id,
                ("freight", "cost"),
            ),
            FigureCheck(
                "transit_days",
                self.transit_days.value,
                self.transit_days.tool_call_id,
                ("transit",),
            ),
        ]


PRICING = Specialist(
    name="pricing",
    system="""\
You price one product for a HALO quote, using the tools provided.

Find a SKU that matches the request, get its price at the requested quantity,
and get the margin policy for that SKU's category. Report the unit price, the
base cost and the margin floor, each with the tool_call_id it came from.

You have no pricing knowledge of your own. Do not compute a margin, do not judge
whether the price is acceptable, and do not adjust anything: report what the
tools returned and let the supervisor decide.""",
    tools=[
        {
            "name": "search_products",
            "description": "Find catalogue SKUs matching a plain-English product description.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["description"],
            },
        },
        {
            "name": "get_price",
            "description": "Unit price and base cost for a SKU at a quantity.",
            "input_schema": {
                "type": "object",
                "properties": {"sku": {"type": "string"}, "quantity": {"type": "integer"}},
                "required": ["sku", "quantity"],
            },
        },
        {
            "name": "get_margin_policy",
            "description": "Floor and target gross margin for a catalogue category.",
            "input_schema": {
                "type": "object",
                "properties": {"category": {"type": "string"}},
                "required": ["category"],
            },
        },
    ],
    routes={
        "search_products": "pim_oms.search_products",
        "get_price": "pim_oms.get_price",
        "get_margin_policy": "pim_oms.get_margin_policy",
    },
    output=PricingReport,
    budget=PRICING_BUDGET,
    required=frozenset({"get_price", "get_margin_policy"}),
)

SUPPLY = Specialist(
    name="supply",
    system="""\
You find who can make a HALO order and when it can ship, using the tools
provided.

Check which suppliers hold the goods and can also decorate them — pass the
decoration method, or you will be given warehouses that cannot do the job. Then
get the decoration charges, and the earliest ship date for the supplier you
chose.

`promised_ship_date` is the `ship_date` the tool returned, copied exactly. Do
not compute it, adjust it, or work backwards from the date the customer asked
for. If the supplier's date is later than the customer's, say so in `notes` and
leave the date alone.""",
    tools=[
        {
            "name": "check_inventory",
            "description": "Which suppliers hold enough of a SKU and can decorate it.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "method": {"type": "string"},
                },
                "required": ["sku", "quantity", "method"],
            },
        },
        {
            "name": "get_decoration_charges",
            "description": "Setup and per-unit run charges for a decoration job.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "method": {"type": "string"},
                    "colors": {"type": "integer"},
                    "units": {"type": "integer"},
                },
                "required": ["method", "colors", "units"],
            },
        },
        {
            "name": "earliest_ship_date",
            "description": "First day a supplier can finish the run, capacity and lead time both.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "supplier_id": {"type": "string"},
                    "method": {"type": "string"},
                    "units": {"type": "integer"},
                    "from_day": {"type": "string", "description": "ISO date"},
                },
                "required": ["supplier_id", "method", "units", "from_day"],
            },
        },
    ],
    routes={
        "check_inventory": "supplier.check_inventory",
        "get_decoration_charges": "supplier.get_decoration_charges",
        "earliest_ship_date": "supplier.earliest_ship_date",
    },
    output=SupplyReport,
    budget=SUPPLY_BUDGET,
    required=frozenset({"get_decoration_charges", "earliest_ship_date"}),
)

LOGISTICS = Specialist(
    name="logistics",
    system="""\
You quote ground freight for a HALO order, using the tool provided.

Estimate freight to the destination state for the number of units on the order,
and report the cost and the transit days with the tool_call_id each came from.
A transit time is not a delivery date: the supervisor adds it to the ship date.""",
    tools=[
        {
            "name": "estimate_freight",
            "description": "Ground transit days and freight cost to a destination state.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "to_state": {"type": "string"},
                    "units": {"type": "integer"},
                    "residential": {"type": "boolean"},
                },
                "required": ["to_state", "units"],
            },
        }
    ],
    routes={"estimate_freight": "shipping.estimate_freight"},
    output=LogisticsReport,
    budget=LOGISTICS_BUDGET,
    required=frozenset({"estimate_freight"}),
)

SPECIALISTS = (PRICING, SUPPLY, LOGISTICS)
"""The three that run a tool loop. Policy is the M3 advisor, run by the supervisor."""
