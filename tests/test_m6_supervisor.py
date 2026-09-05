"""M6: delegation, the margin gate, and an approval that resumes rather than
restarts.

The deliverable is a low-margin quote that pauses and then completes. The
done-when is that a budget running out yields an escalation with a reason and
never a truncated answer. Both are here, and so is the property that makes the
first one worth anything: resuming does not call a model or a tool. That is
asserted with a client and a gateway that raise if they are touched, because a
resume that quietly re-ran everything would pass every other test in this file.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from fakes import SpecialistModel
from halo.agents import supervisor
from halo.agents.specialists import LOGISTICS, PRICING, SUPPLY
from halo.domain.request import QuoteRequest
from halo.mcp_servers import accounts, pim_oms, shipping, supplier
from halo.platform.budget import Budget
from halo.platform.checkpoint import (
    ApprovalError,
    Checkpoint,
    FileCheckpointStore,
    approve,
    may_approve,
)
from halo.platform.gateway import InProcessGateway, ToolSpec
from halo.platform.identity import Principal, Role
from halo.platform.outcome import OutcomeStatus

TINY = Budget(wall_clock_seconds=120, max_tokens=100, max_tool_calls=8, max_usd=Decimal("0.25"))
"""Enough for one call and not two, so the second one raises."""

THIN_SKU = "HL-BAG-1007"
"""Priced to win volume: 25.4% margin at 500, under the 31% bags floor."""

HEALTHY_SKU = "HL-KNT-1005"
"""The standard ladder: 36.7% at 500, comfortably clear of the 30% knits floor."""

SELLER = Principal(
    user_id="usr-mwes01",
    tenant_id="tnt-mwest1",
    role=Role.SELLER,
    account_ids=("acct-mwes02",),
)
MANAGER = Principal(
    user_id="usr-mwes00",
    tenant_id="tnt-mwest1",
    role=Role.SALES_MANAGER,
    account_ids=("acct-mwes02",),
)
OTHER_TENANT_MANAGER = Principal(
    user_id="usr-nea000",
    tenant_id="tnt-neast1",
    role=Role.SALES_MANAGER,
    account_ids=("acct-mwes02",),
)

FUNCTIONS = {
    "accounts.get_account": accounts.get_account,
    "pim_oms.search_products": pim_oms.search_products,
    "pim_oms.get_price": pim_oms.get_price,
    "pim_oms.get_margin_policy": pim_oms.get_margin_policy,
    "supplier.check_inventory": supplier.check_inventory,
    "supplier.get_decoration_charges": supplier.get_decoration_charges,
    "supplier.earliest_ship_date": supplier.earliest_ship_date,
    "shipping.estimate_freight": shipping.estimate_freight,
}


def a_request(**overrides) -> QuoteRequest:
    return QuoteRequest(
        **{
            "account_id": "acct-mwes02",
            "product_description": "duffel bags",
            "quantity": 500,
            "decoration_method": "screen_print",
            "imprint_colors": 3,
            "ship_to_state": "IL",
            **overrides,
        }
    )


def a_gateway() -> InProcessGateway:
    return InProcessGateway(
        functions=FUNCTIONS,
        allowed={name: ToolSpec(name, 20) for name in FUNCTIONS},
        principal=SELLER,
    )


class Exploding:
    """A client or gateway that fails the test if anything reaches it."""

    def __getattr__(self, name):
        raise AssertionError(f"resume must not call {name}")


@pytest.fixture
def store(tmp_path) -> FileCheckpointStore:
    return FileCheckpointStore(tmp_path / "checkpoints")


async def run(sku: str, store, **overrides):
    gateway = a_gateway()
    client = SpecialistModel(gateway, sku=sku, quantity=500)
    outcome = await supervisor.draft(
        a_request(**overrides),
        principal=SELLER,
        client=client,
        gateway=gateway,
        store=store,
    )
    return outcome, gateway, client


class TestDelegation:
    async def test_each_specialist_reports_in_turn(self, store):
        _, _, client = await run(HEALTHY_SKU, store)
        assert client.parsed == ["PricingReport", "SupplyReport", "LogisticsReport"]

    async def test_a_healthy_margin_completes_without_approval(self, store):
        outcome, _, _ = await run(HEALTHY_SKU, store)

        assert outcome.status is OutcomeStatus.COMPLETED
        assert outcome.next_state == "ready_to_send"
        assert store.open_checkpoints() == []

    async def test_every_figure_on_the_quote_carries_a_citation(self, store):
        outcome, gateway, _ = await run(HEALTHY_SKU, store)
        refs = {citation["ref"] for citation in outcome.payload["lines"][0]["citations"]}

        assert refs <= {call.id for call in gateway.audit}
        assert outcome.evidence

    async def test_the_specialists_have_separate_budgets(self):
        """Not a shared pool. A runaway pricing loop must not spend supply's
        allowance, which is the whole reason for splitting the loop."""
        budgets = [PRICING.budget, SUPPLY.budget, LOGISTICS.budget]
        assert len({budget.model_dump_json() for budget in budgets}) == 3

    async def test_the_run_reports_what_every_specialist_spent(self, store):
        outcome, _, _ = await run(HEALTHY_SKU, store)
        assert outcome.usage.input_tokens > 0
        assert outcome.usage.tool_calls == 7


class TestTheMarginGate:
    async def test_a_low_margin_quote_pauses(self, store):
        outcome, _, _ = await run(THIN_SKU, store)

        assert outcome.status is OutcomeStatus.ESCALATED
        assert outcome.next_state == "awaiting_margin_approval"
        assert "below the 31.0% floor" in outcome.escalation_reason

    async def test_the_pause_carries_no_quote(self, store):
        """A quote nobody approved must not be sittable-on. The payload names the
        checkpoint and the numbers, and nothing that could be sent."""
        outcome, _, _ = await run(THIN_SKU, store)

        assert set(outcome.payload) == {"checkpoint_id", "margin_pct", "floor_pct"}
        assert outcome.payload["margin_pct"] == "25.4"

    async def test_the_checkpoint_holds_the_evidence(self, store):
        outcome, gateway, _ = await run(THIN_SKU, store)
        checkpoint = store.load(outcome.payload["checkpoint_id"])

        assert set(checkpoint.reports) == {"pricing", "supply", "logistics", "policy"}
        assert len(checkpoint.calls) == len(gateway.audit)
        assert checkpoint.principal["user_id"] == SELLER.user_id
        assert checkpoint.open

    async def test_a_notification_is_sent(self, store, capsys):
        outcome, _, _ = await run(THIN_SKU, store)
        printed = capsys.readouterr().out

        assert "[approval]" in printed
        assert outcome.payload["checkpoint_id"] in printed

    def test_the_margin_is_computed_from_sourced_numbers_only(self):
        assert supervisor.margin_pct(Decimal("25.78"), Decimal("19.24")) == Decimal("25.4")
        assert supervisor.margin_pct(Decimal("0"), Decimal("5")) == Decimal("0.0")


class TestApproval:
    async def test_a_manager_releases_it_and_the_quote_completes(self, store):
        paused, _, _ = await run(THIN_SKU, store)
        checkpoint = store.load(paused.payload["checkpoint_id"])

        released = approve(checkpoint, MANAGER)
        store.save(released)
        outcome = supervisor.resume(released)

        assert outcome.status is OutcomeStatus.COMPLETED
        assert outcome.payload["approved_by"] == MANAGER.user_id
        assert outcome.payload["margin_pct"] == "25.4"

    async def test_resuming_calls_no_model_and_no_tool(self, store):
        """Rule 06, structurally. `resume` takes neither a client nor a gateway,
        so this test is really asserting that its signature stays that way — and
        it would fail loudly if someone added one and reached for it."""
        paused, _, _ = await run(THIN_SKU, store)
        released = approve(store.load(paused.payload["checkpoint_id"]), MANAGER)

        import inspect

        parameters = inspect.signature(supervisor.resume).parameters
        assert set(parameters) == {"checkpoint"}
        assert supervisor.resume(released).status is OutcomeStatus.COMPLETED

    async def test_the_resumed_quote_is_the_one_that_was_approved(self, store):
        """Same figures, not re-fetched ones. Re-running would produce a quote
        with a different ship date the moment the capacity calendar moved."""
        paused, _, _ = await run(THIN_SKU, store)
        checkpoint = store.load(paused.payload["checkpoint_id"])
        outcome = supervisor.resume(approve(checkpoint, MANAGER))

        supply = checkpoint.reports["supply"]
        assert outcome.payload["promised_ship_date"] == supply["promised_ship_date"]
        assert (
            outcome.payload["lines"][0]["unit_price"]
            == checkpoint.reports["pricing"]["unit_price"]["value"]
        )

    async def test_an_unapproved_checkpoint_does_not_resume(self, store):
        paused, _, _ = await run(THIN_SKU, store)
        outcome = supervisor.resume(store.load(paused.payload["checkpoint_id"]))

        assert outcome.status is OutcomeStatus.ESCALATED
        assert "has not been approved" in outcome.escalation_reason

    async def test_the_work_resumes_inside_the_sellers_scope(self, store):
        """The approver unlocks the quote; they do not take it over."""
        paused, _, _ = await run(THIN_SKU, store)
        checkpoint = store.load(paused.payload["checkpoint_id"])
        outcome = supervisor.resume(approve(checkpoint, MANAGER))

        assert checkpoint.owner().user_id == SELLER.user_id
        assert outcome.payload["account_id"] in SELLER.account_ids


class TestWhoMayApprove:
    def a_checkpoint(self, **overrides) -> Checkpoint:
        return Checkpoint(
            **{
                "id": "chk-test",
                "created_at": "2026-09-05T00:00:00+00:00",
                "reason": "margin 25.4% is below the 30.0% floor for knits",
                "request": {},
                "principal": SELLER.model_dump(mode="json"),
                "reports": {},
                "calls": [],
                "margin_pct": "25.4",
                "floor_pct": "30.0",
                "usage": {},
                **overrides,
            }
        )

    def test_a_sales_manager_in_the_same_tenant_may(self):
        may_approve(self.a_checkpoint(), MANAGER)

    def test_the_seller_who_raised_it_may_not(self):
        """Self-approval would make the gate a formality with a log entry."""
        with pytest.raises(ApprovalError, match="seller who raised it"):
            may_approve(self.a_checkpoint(), SELLER)

    def test_another_seller_may_not(self):
        peer = SELLER.model_copy(update={"user_id": "usr-mwes04"})
        with pytest.raises(ApprovalError, match="cannot approve"):
            may_approve(self.a_checkpoint(), peer)

    def test_a_manager_from_another_tenant_may_not(self):
        with pytest.raises(ApprovalError, match="another tenant"):
            may_approve(self.a_checkpoint(), OTHER_TENANT_MANAGER)

    def test_it_cannot_be_approved_twice(self):
        with pytest.raises(ApprovalError, match="already approved"):
            may_approve(self.a_checkpoint(approved_by="usr-mwes00"), MANAGER)


class TestBudgetExhaustion:
    """The done-when: escalated with a reason, never a truncated answer."""

    async def test_a_specialist_out_of_budget_escalates_by_name(self, store, monkeypatch):
        monkeypatch.setattr(supervisor, "PRICING", replace(PRICING, budget=TINY))
        outcome, _, _ = await run(HEALTHY_SKU, store)

        assert outcome.status is OutcomeStatus.ESCALATED
        assert "pricing exhausted its budget" in outcome.escalation_reason
        assert "max_tokens" in outcome.escalation_reason

    async def test_nothing_partial_travels_with_the_escalation(self, store, monkeypatch):
        monkeypatch.setattr(supervisor, "SUPPLY", replace(SUPPLY, budget=TINY))
        outcome, _, _ = await run(HEALTHY_SKU, store)

        assert outcome.status is OutcomeStatus.ESCALATED
        assert outcome.payload is None
        assert outcome.next_state == "await_budget_increase"

    async def test_the_run_stops_at_the_specialist_that_failed(self, store, monkeypatch):
        """Logistics never runs. A supervisor that carried on would spend money
        on a quote it already could not assemble."""
        monkeypatch.setattr(supervisor, "PRICING", replace(PRICING, budget=TINY))
        _, _, client = await run(HEALTHY_SKU, store)

        assert client.parsed == []


class TestTheCommands:
    """`pending` and `approve` over a real store, with no model in the path."""

    def test_pending_lists_open_checkpoints_only(self, tmp_path, monkeypatch, capsys):
        from halo import cli
        from halo.platform import checkpoint as checkpoint_module

        monkeypatch.setattr(checkpoint_module, "CHECKPOINT_DIR", tmp_path)
        store = FileCheckpointStore(tmp_path)
        waiting = TestWhoMayApprove().a_checkpoint(id="chk-open")
        done = TestWhoMayApprove().a_checkpoint(id="chk-done", approved_by="usr-mwes00")
        store.save(waiting)
        store.save(done)

        assert cli.main(["pending"]) == 0
        printed = capsys.readouterr().out
        assert "chk-open" in printed
        assert "chk-done" not in printed

    def test_approve_refuses_a_seller_and_says_why(self, tmp_path, monkeypatch, capsys):
        from halo import cli
        from halo.platform import checkpoint as checkpoint_module

        monkeypatch.setattr(checkpoint_module, "CHECKPOINT_DIR", tmp_path)
        FileCheckpointStore(tmp_path).save(TestWhoMayApprove().a_checkpoint(id="chk-seller"))

        assert cli.main(["approve", "chk-seller"]) == 1
        assert "seller who raised it" in capsys.readouterr().err
