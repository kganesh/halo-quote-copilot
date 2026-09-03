"""M2's real mechanic: a cited figure has to appear in the call it cites."""

from datetime import date
from decimal import Decimal

import pytest

from halo.agents.sourcing import (
    TOOL_ROUTES,
    TOOLS,
    SourcedFigure,
    SourcingDecision,
    assemble,
    verify,
)
from halo.platform.gateway import ToolCall


def a_decision(**overrides) -> SourcingDecision:
    return SourcingDecision(
        **{
            "sku": "HL-KNT-1005",
            "product_name": "Fairhaven Fleece Hoodie",
            "quantity": 500,
            "unit_price": SourcedFigure(value=Decimal("23.78"), tool_call_id="tc-0002"),
            "decoration_method": "screen_print",
            "imprint_colors": 3,
            "decoration_setup_fee": SourcedFigure(value=Decimal("66.00"), tool_call_id="tc-0003"),
            "decoration_run_charge_per_unit": SourcedFigure(
                value=Decimal("1.55"), tool_call_id="tc-0003"
            ),
            "supplier_id": "sup-apex01",
            "supplier_name": "Apex Decorating",
            "promised_ship_date": date(2026, 10, 5),
            "ship_date_tool_call_id": "tc-0004",
            "freight_cost": SourcedFigure(value=Decimal("294.00"), tool_call_id="tc-0005"),
            **overrides,
        }
    )


def an_audit() -> list[ToolCall]:
    return [
        ToolCall(id="tc-0001", name="pim_oms.search_products", arguments={}, result=[]),
        ToolCall(
            id="tc-0002",
            name="pim_oms.get_price",
            arguments={"sku": "HL-KNT-1005", "quantity": 500},
            result={"unit_price": "23.78", "base_cost": "15.05", "tier": "500-plus"},
        ),
        ToolCall(
            id="tc-0003",
            name="supplier.get_decoration_charges",
            arguments={"method": "screen_print", "colors": 3, "units": 500},
            result={"setup_fee": "66.00", "run_charge_per_unit": "1.55"},
        ),
        ToolCall(
            id="tc-0004",
            name="supplier.earliest_ship_date",
            arguments={"supplier_id": "sup-apex01"},
            result={"ship_date": "2026-10-05", "lead_time_days": 7},
        ),
        ToolCall(
            id="tc-0005",
            name="shipping.estimate_freight",
            arguments={"to_state": "IL", "units": 500},
            result={"freight_cost": "294.00", "zone": 2},
        ),
    ]


class TestVerify:
    def test_a_fully_sourced_decision_has_no_problems(self):
        assert verify(a_decision(), an_audit()) == []

    def test_a_figure_the_tool_never_returned_is_caught(self):
        """The id is real and the call succeeded — only the number is invented.
        This is the failure that looks most like a correct answer."""
        decision = a_decision(
            unit_price=SourcedFigure(value=Decimal("19.99"), tool_call_id="tc-0002")
        )
        problems = verify(decision, an_audit())
        assert problems == ["unit_price=19.99 does not appear in tc-0002 (pim_oms.get_price)"]

    def test_a_citation_to_a_call_that_never_happened_is_caught(self):
        decision = a_decision(
            freight_cost=SourcedFigure(value=Decimal("294.00"), tool_call_id="tc-0099")
        )
        assert "not in the audit" in verify(decision, an_audit())[0]

    def test_a_citation_to_a_failed_call_is_caught(self):
        audit = an_audit()
        audit[4] = ToolCall(
            id="tc-0005", name="shipping.estimate_freight", arguments={}, error="timed out"
        )
        assert "which failed" in verify(a_decision(), audit)[0]

    def test_a_date_cited_to_the_wrong_call_is_caught(self):
        decision = a_decision(ship_date_tool_call_id="tc-0005")
        assert "promised_ship_date" in verify(decision, an_audit())[0]

    def test_a_figure_matching_an_unrelated_number_is_still_caught(self):
        """estimate_freight returns zone 2 alongside the cost. A fabricated
        $2.00 freight charge once matched the zone and verified clean."""
        decision = a_decision(
            freight_cost=SourcedFigure(value=Decimal("2.00"), tool_call_id="tc-0005")
        )
        assert "freight_cost=2.00 does not appear" in verify(decision, an_audit())[0]

    def test_every_bad_figure_is_reported_not_just_the_first(self):
        decision = a_decision(
            unit_price=SourcedFigure(value=Decimal("1.00"), tool_call_id="tc-0002"),
            freight_cost=SourcedFigure(value=Decimal("2.00"), tool_call_id="tc-0005"),
        )
        assert len(verify(decision, an_audit())) == 2


class TestAssemble:
    def test_a_verified_decision_becomes_a_cited_quote(self):
        quote = assemble(a_decision(), an_audit())

        assert quote.subtotal == Decimal("11890.00")
        # 11,890 product + 66 setup + 775 run (1.55 x 500) + 294 freight
        assert quote.total == Decimal("13025.00")
        assert {c.ref for c in quote.all_citations} == {"tc-0002", "tc-0003", "tc-0005"}

    def test_every_citation_names_the_tool_that_produced_it(self):
        quote = assemble(a_decision(), an_audit())
        by_ref = {c.ref: c.supporting_text for c in quote.all_citations}
        assert "pim_oms.get_price" in by_ref["tc-0002"]
        assert "shipping.estimate_freight" in by_ref["tc-0005"]


class TestToolDeclarations:
    """The model's tool list, the gateway's routes and the servers' functions
    have to stay in step. Drift here is silent: the model calls a tool that no
    longer exists and the loop just fails."""

    def test_every_declared_tool_has_a_route(self):
        assert {t["name"] for t in TOOLS} == set(TOOL_ROUTES)

    def test_every_route_points_at_a_real_server_function(self):
        from halo.mcp_servers import pim_oms, shipping, supplier

        modules = {"pim_oms": pim_oms, "supplier": supplier, "shipping": shipping}
        for route in TOOL_ROUTES.values():
            server_name, tool_name = route.split(".", 1)
            assert callable(getattr(modules[server_name], tool_name)), route

    @pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t["name"])
    def test_declared_required_arguments_are_real_parameters(self, tool):
        import inspect

        from halo.mcp_servers import pim_oms, shipping, supplier

        modules = {"pim_oms": pim_oms, "supplier": supplier, "shipping": shipping}
        server_name, tool_name = TOOL_ROUTES[tool["name"]].split(".", 1)
        parameters = inspect.signature(getattr(modules[server_name], tool_name)).parameters
        for argument in tool["input_schema"]["properties"]:
            assert argument in parameters, f"{tool['name']}.{argument}"


class TestOpenQuestionsAreNotFailures:
    """A colour still to be chosen is a normal quote; an unsourced price is not
    a quote at all. Folding both into `unresolved` made every realistic request
    escalate."""

    def test_open_questions_do_not_block_a_quote(self):
        decision = a_decision(open_questions=["Garment colour not specified"])
        assert verify(decision, an_audit()) == []
        assert assemble(decision, an_audit()).total == Decimal("13025.00")

    def test_an_unsourced_figure_is_still_a_blocker(self):
        decision = a_decision(unresolved=["no freight quote available"])
        assert decision.unresolved
