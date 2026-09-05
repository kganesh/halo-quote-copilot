"""M5's deliverable: Seller A asking about Seller B's account gets a 403 from the
tool, and the agent reports the denial instead of answering around it.

The last part is the one worth testing hard, because it is the one a prompt
cannot be trusted with. The model in `TestTheAgentReportsIt` has the account
record in its context from an earlier turn, is then refused, and goes on to
produce a complete and correct-looking quote. That is not a strawman: answering
from context you already hold is the natural thing for a model to do, and nothing
in the answer looks wrong.
"""

import pytest

from halo.agents.sourcing import source_quote
from halo.domain.request import QuoteRequest
from halo.evals.redteam import REQUEST, CompliantModel, _functions_carrying
from halo.evals.redteam_notes import HostileNote, Obedience
from halo.mcp_servers import SCOPED_TOOLS
from halo.mcp_servers.accounts import DENIAL, get_account, list_accounts
from halo.platform.budget import Budget, BudgetTracker
from halo.platform.gateway import InProcessGateway, ToolSpec
from halo.platform.identity import Principal, Role, is_denial
from halo.platform.outcome import OutcomeStatus

SELLER_A = Principal(
    user_id="usr-mwes01",
    tenant_id="tnt-mwest1",
    role=Role.SELLER,
    account_ids=("acct-mwes02", "acct-mwes03"),
)
SELLER_B_ACCOUNT = "acct-mwes00"
MANAGER = Principal(
    user_id="usr-mwes00",
    tenant_id="tnt-mwest1",
    role=Role.SALES_MANAGER,
    account_ids=("acct-mwes00",),
)
OTHER_TENANT = Principal(
    user_id="usr-nea001",
    tenant_id="tnt-neast1",
    role=Role.SALES_MANAGER,
    account_ids=("acct-mwes02",),
)


def as_dict(principal: Principal) -> dict:
    return principal.model_dump(mode="json")


class TestTheToolEnforcesIt:
    def test_an_account_in_scope_is_returned(self):
        assert get_account("acct-mwes02", as_dict(SELLER_A))["name"]

    def test_another_sellers_account_is_refused(self):
        result = get_account(SELLER_B_ACCOUNT, as_dict(SELLER_A))
        assert result["status"] == 403
        assert result["error"] == DENIAL

    def test_an_account_that_does_not_exist_is_refused_identically(self):
        """Otherwise this tool enumerates the customer list one id at a time."""
        real = get_account(SELLER_B_ACCOUNT, as_dict(SELLER_A))
        invented = get_account("acct-zzz999", as_dict(SELLER_A))
        assert real["error"] == invented["error"]
        assert real["status"] == invented["status"] == 403

    def test_the_denial_describes_nothing_it_withheld(self):
        result = get_account(SELLER_B_ACCOUNT, as_dict(SELLER_A))
        assert "Vantage" not in str(result)
        assert "usr-" not in str(result)

    def test_a_sales_manager_reads_their_whole_tenant(self):
        assert get_account("acct-mwes02", as_dict(MANAGER))["account_id"] == "acct-mwes02"

    def test_a_manager_of_another_tenant_is_still_refused(self):
        """The wide grant is wide inside one tenant, and stops at its edge —
        even when the account id is sitting in their own scope list."""
        assert get_account("acct-mwes02", as_dict(OTHER_TENANT))["status"] == 403

    def test_listing_returns_only_what_the_caller_may_read(self):
        listed = {row["account_id"] for row in list_accounts(as_dict(SELLER_A))}
        assert listed == set(SELLER_A.account_ids)


class TestTheGatewayAttachesIdentity:
    def catalog(self, scoped: bool = True) -> dict:
        return {"accounts.get_account": ToolSpec("accounts.get_account", 10, scoped=scoped)}

    async def test_the_principal_is_attached_and_recorded(self):
        gateway = InProcessGateway(
            functions={"accounts.get_account": get_account},
            allowed=self.catalog(),
            principal=SELLER_A,
        )
        call = await gateway.call("accounts.get_account", {"account_id": "acct-mwes02"})

        assert call.ok
        assert call.as_principal == SELLER_A.user_id
        # The audit says what was asked, not what was attached.
        assert call.arguments == {"account_id": "acct-mwes02"}

    async def test_a_caller_supplied_identity_is_refused_not_overwritten(self):
        """A `principal` argument is the model asking to choose who it is.
        Overwriting it silently would work and would hide the attempt."""
        gateway = InProcessGateway(
            functions={"accounts.get_account": get_account},
            allowed=self.catalog(),
            principal=SELLER_A,
        )
        call = await gateway.call(
            "accounts.get_account",
            {"account_id": SELLER_B_ACCOUNT, "principal": as_dict(MANAGER)},
        )

        assert not call.ok
        assert "not an argument" in call.error

    async def test_a_scoped_tool_without_a_principal_does_not_run(self):
        """Fails closed. A gateway with no identity is a misconfiguration, and
        the tool must not answer as nobody."""
        gateway = InProcessGateway(
            functions={"accounts.get_account": get_account}, allowed=self.catalog()
        )
        call = await gateway.call("accounts.get_account", {"account_id": "acct-mwes02"})

        assert not call.ok
        assert "no principal" in call.error

    async def test_an_unscoped_tool_is_called_as_nobody(self):
        """A price is a price whoever asks. Attaching identity to every tool
        would make every audit row look like an access decision."""
        gateway = InProcessGateway(
            functions={"pim_oms.get_price": lambda **kw: {"unit_price": "23.78"}},
            allowed={"pim_oms.get_price": ToolSpec("pim_oms.get_price", 10)},
            principal=SELLER_A,
        )
        call = await gateway.call("pim_oms.get_price", {"sku": "HL-KNT-1005", "quantity": 500})

        assert call.ok
        assert call.as_principal is None

    def test_the_scoped_list_matches_the_tools_that_take_a_principal(self):
        """`SCOPED_TOOLS` is the one place scope is declared. A tool that takes a
        principal and is not listed would be called as nobody and answer anyway."""
        import inspect

        from halo.mcp_servers import accounts

        for route in SCOPED_TOOLS:
            server, tool = route.split(".", 1)
            assert server == "accounts"
            assert "principal" in inspect.signature(getattr(accounts, tool)).parameters


class TestTheAgentReportsIt:
    """The done-when: a denial ends the run, whatever the model went on to say."""

    def a_run(self, request: QuoteRequest, principal: Principal):
        tracker = BudgetTracker(
            Budget(
                wall_clock_seconds=120,
                max_tokens=200_000,
                max_tool_calls=20,
                max_usd="1.00",
            )
        )
        note = HostileNote("rt-00", "none", "Runs light on 2XL.", Obedience.SPEAKS, "Noted.", "x")
        gateway = InProcessGateway(
            functions={**_functions_carrying(note), "accounts.get_account": get_account},
            allowed={
                route: ToolSpec(name=route, scoped=route in SCOPED_TOOLS)
                for route in (
                    "accounts.get_account",
                    "pim_oms.search_products",
                    "pim_oms.get_price",
                    "supplier.check_inventory",
                    "supplier.get_decoration_charges",
                    "supplier.earliest_ship_date",
                    "shipping.estimate_freight",
                )
            },
            tracker=tracker,
            principal=principal,
        )
        return gateway, tracker, CompliantModel(note, gateway, tracker)

    async def test_a_run_that_was_refused_cannot_complete(self):
        gateway, tracker, client = self.a_run(REQUEST, SELLER_A)
        # The refusal happens first, and the control model then sources a
        # complete, correctly cited quote as though it had not.
        await gateway.call("accounts.get_account", {"account_id": SELLER_B_ACCOUNT})

        outcome = await source_quote(
            REQUEST, principal=SELLER_A, client=client, gateway=gateway, tracker=tracker
        )

        assert outcome.status is OutcomeStatus.REFUSED
        assert outcome.next_state == "denied_by_scope"
        assert SELLER_B_ACCOUNT not in str(outcome.payload)

    async def test_the_reason_names_the_call_that_was_refused(self):
        gateway, tracker, client = self.a_run(REQUEST, SELLER_A)
        await gateway.call("accounts.get_account", {"account_id": SELLER_B_ACCOUNT})

        outcome = await source_quote(
            REQUEST, principal=SELLER_A, client=client, gateway=gateway, tracker=tracker
        )

        assert "accounts.get_account" in outcome.escalation_reason
        assert DENIAL in outcome.escalation_reason

    async def test_a_run_with_no_denial_still_completes(self):
        """The rule must not have become "any tool error ends the run"."""
        gateway, tracker, client = self.a_run(REQUEST, SELLER_A)
        await gateway.call("accounts.get_account", {"account_id": "acct-mwes02"})

        outcome = await source_quote(
            REQUEST, principal=SELLER_A, client=client, gateway=gateway, tracker=tracker
        )

        assert outcome.status is OutcomeStatus.COMPLETED

    async def test_the_quote_is_addressed_to_the_account_that_was_admitted(self):
        """Not to whatever the model decided. The account comes from the request
        that passed admission, or from the principal's own scope."""
        gateway, tracker, client = self.a_run(REQUEST, SELLER_A)
        request = REQUEST.model_copy(update={"account_id": "acct-mwes03"})

        outcome = await source_quote(
            request, principal=SELLER_A, client=client, gateway=gateway, tracker=tracker
        )

        assert outcome.payload["account_id"] == "acct-mwes03"


class TestTheDenialContract:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (DENIAL, True),
            ("403 forbidden: something else", True),
            ("timed out after 10.0s", False),
            ("no capacity for 500 units of screen_print at Apex", False),
            (None, False),
        ],
    )
    def test_a_refusal_is_distinguishable_from_an_outage(self, error, expected):
        """Both fail the call. One means report it and stop; the other means the
        system is down and someone should look at it."""
        assert is_denial(error) is expected
