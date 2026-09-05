"""The three rules the rest of the system depends on, tested without a model."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from halo.domain.quote import Citation, CitationKind, DecorationCharge, Quote, QuoteLine
from halo.platform.budget import Budget, BudgetExceeded, BudgetTracker
from halo.platform.identity import Principal, Role, ScopeDenied
from halo.platform.outcome import Outcome, OutcomeStatus


def a_principal(**overrides) -> Principal:
    return Principal(
        **{
            "user_id": "usr-mwest01",
            "tenant_id": "tnt-mwest1",
            "role": Role.SELLER,
            "account_ids": ("acct-mwest00", "acct-mwest01"),
            **overrides,
        }
    )


class TestIdentity:
    def test_seller_reads_only_their_own_book(self):
        p = a_principal()
        assert p.may_read_account("acct-mwest00")
        assert not p.may_read_account("acct-mwest07")

    def test_manager_reads_the_whole_tenant(self):
        p = a_principal(role=Role.SALES_MANAGER)
        assert p.may_read_account("acct-mwest07")

    def test_principal_cannot_be_widened_after_minting(self):
        p = a_principal()
        with pytest.raises(ValidationError):
            p.account_ids = ("acct-anything",)

    def test_denial_does_not_leak_the_record_it_refused(self):
        p = a_principal()
        err = ScopeDenied(p, "acct-neast03")
        assert "acct-neast03" in str(err)
        assert err.user_id == p.user_id


class TestBudget:
    def _budget(self, **overrides) -> Budget:
        return Budget(
            **{
                "wall_clock_seconds": 30,
                "max_tokens": 10_000,
                "max_tool_calls": 8,
                "max_usd": Decimal("0.50"),
                **overrides,
            }
        )

    def test_a_run_inside_every_ceiling_passes(self):
        tracker = BudgetTracker(self._budget(), now=lambda: 0.0)
        tracker.record_model_call(100, 200, Decimal("0.01"))
        tracker.record_tool_call()
        tracker.check()

    def test_wall_clock_is_enforced(self):
        clock = iter([0.0, 31.0, 31.0])
        tracker = BudgetTracker(self._budget(), now=lambda: next(clock))
        with pytest.raises(BudgetExceeded) as exc:
            tracker.check()
        assert exc.value.dimension == "wall_clock_seconds"

    def test_tokens_are_enforced(self):
        tracker = BudgetTracker(self._budget(max_tokens=500), now=lambda: 0.0)
        tracker.record_model_call(400, 200, Decimal("0.01"))
        with pytest.raises(BudgetExceeded) as exc:
            tracker.check()
        assert exc.value.dimension == "max_tokens"

    def test_dollars_are_enforced(self):
        tracker = BudgetTracker(self._budget(max_usd=Decimal("0.02")), now=lambda: 0.0)
        tracker.record_model_call(10, 10, Decimal("0.03"))
        with pytest.raises(BudgetExceeded) as exc:
            tracker.check()
        assert exc.value.dimension == "max_usd"

    def test_tool_calls_are_enforced(self):
        tracker = BudgetTracker(self._budget(max_tool_calls=1), now=lambda: 0.0)
        tracker.record_tool_call()
        tracker.record_tool_call()
        with pytest.raises(BudgetExceeded) as exc:
            tracker.check()
        assert exc.value.dimension == "max_tool_calls"


class TestOutcome:
    def test_a_stop_must_say_why(self):
        with pytest.raises(ValidationError, match="escalation_reason"):
            Outcome(status=OutcomeStatus.ESCALATED, agent="pricing")

    def test_a_refusal_must_say_why(self):
        with pytest.raises(ValidationError, match="escalation_reason"):
            Outcome(status=OutcomeStatus.REFUSED, agent="pricing")

    def test_completion_must_carry_a_payload(self):
        with pytest.raises(ValidationError, match="payload"):
            Outcome(status=OutcomeStatus.COMPLETED, agent="pricing")

    def test_a_well_formed_escalation(self):
        outcome = Outcome(
            status=OutcomeStatus.ESCALATED,
            agent="assembler",
            escalation_reason="margin 27.4% is below the 30.0% knits floor",
            next_state="await_manager_approval",
        )
        assert outcome.next_state == "await_manager_approval"


def a_citation(ref: str = "tc-0001") -> Citation:
    return Citation(
        kind=CitationKind.TOOL_CALL,
        ref=ref,
        supporting_text="unit_price 12.40 at qty 500 for HL-KNT-1004",
    )


class TestQuoteProvenance:
    def _quote(self, **overrides) -> Quote:
        return Quote(
            **{
                "request_id": "req-0001",
                "account_id": "acct-mwest00",
                "lines": [
                    QuoteLine(
                        sku="HL-KNT-1004",
                        description="Fleece Hoodie, Navy",
                        quantity=500,
                        unit_price=Decimal("12.40"),
                        citations=[a_citation()],
                    )
                ],
                "decoration": DecorationCharge(
                    method="screen_print",
                    colors=3,
                    setup_fee=Decimal("66.00"),
                    run_charge_per_unit=Decimal("1.55"),
                    citations=[
                        Citation(
                            kind=CitationKind.CHUNK,
                            ref="atl-screen-print-standards#charges",
                            supporting_text="Screen setup: $22.00 per colour, per location.",
                        )
                    ],
                ),
                "shipping_cost": Decimal("240.00"),
                "shipping_citations": [a_citation("tc-0002")],
                "promised_ship_date": date(2026, 10, 6),
                "margin_pct": Decimal("34.2"),
                **overrides,
            }
        )

    def test_a_fully_cited_quote_is_accepted(self):
        quote = self._quote()
        assert quote.subtotal == Decimal("6200.00")
        assert quote.total == Decimal("7281.00")

    def test_an_uncited_line_is_rejected(self):
        with pytest.raises(ValidationError):
            self._quote(
                lines=[
                    QuoteLine(
                        sku="HL-KNT-1004",
                        description="Fleece Hoodie, Navy",
                        quantity=500,
                        unit_price=Decimal("12.40"),
                        citations=[],
                    )
                ]
            )

    def test_a_citation_with_no_supporting_text_is_rejected(self):
        with pytest.raises(ValidationError):
            Citation(kind=CitationKind.CHUNK, ref="atl-x", supporting_text="   ")

    def test_a_document_id_filed_as_a_tool_call_is_rejected(self):
        """This looks correct in a quote and resolves to nothing in the audit
        trail."""
        with pytest.raises(ValidationError, match="should start with"):
            self._quote(
                shipping_citations=[
                    Citation(
                        kind=CitationKind.TOOL_CALL,
                        ref="atl-freight-and-zones#zone-transit",
                        supporting_text="Zone 4: 3 business days.",
                    )
                ]
            )

    def test_all_citations_collects_every_source(self):
        assert len(self._quote().all_citations) == 3


class TestABudgetSaysWhoseItIs:
    """A run has several budgets open at once. A breach that does not name one
    sends someone to raise a limit that was never reached."""

    def a_tracker(self, owner: str) -> BudgetTracker:
        return BudgetTracker(
            Budget(
                wall_clock_seconds=99,
                max_tokens=10,
                max_tool_calls=0,
                max_usd=Decimal("0.01"),
            ),
            owner=owner,
        )

    def test_the_owner_is_on_the_exception_and_in_its_message(self):
        tracker = self.a_tracker("pricing")
        tracker.record_model_call(50, 50, Decimal("0"))

        with pytest.raises(BudgetExceeded) as exc:
            tracker.check()

        assert exc.value.owner == "pricing"
        assert "pricing budget exceeded on max_tokens" in str(exc.value)

    def test_the_default_owner_is_the_run(self):
        """What a caller who does not care about the distinction is holding."""
        tracker = BudgetTracker(
            Budget(wall_clock_seconds=99, max_tokens=1, max_tool_calls=0, max_usd=Decimal("1"))
        )
        tracker.record_model_call(5, 5, Decimal("0"))

        with pytest.raises(BudgetExceeded) as exc:
            tracker.check()
        assert exc.value.owner == "run"


class TestAnUnpricedModelCannotRun:
    """A dollar budget is only a limit if every call can be priced.

    An unpriced call adds zero to `usage.usd`, so `max_usd` is never reached and
    the run proceeds under a cap that has stopped existing. Nothing errors, and
    the ledger reports $0.00 for a run that really spent money.
    """

    def test_a_model_with_no_rate_card_entry_is_refused_at_construction(self):
        from halo.platform.bedrock import BedrockClient, UnpricedModel

        with pytest.raises(UnpricedModel, match="no rate card entry"):
            BedrockClient(model="us.anthropic.claude-nonesuch-9")

    def test_the_message_says_how_to_fix_it(self):
        from halo.platform.bedrock import UnpricedModel

        message = str(UnpricedModel("us.anthropic.claude-nonesuch-9"))
        assert "PRICE_PER_MTOK" in message
        assert "list-foundation-model-agreement-offers" in message

    def test_it_reads_as_a_setup_problem_not_a_crash(self):
        """A RuntimeError, so the CLI's existing handlers already catch it."""
        from halo.platform.bedrock import UnpricedModel

        assert issubclass(UnpricedModel, RuntimeError)

    @pytest.mark.parametrize(
        "model",
        [
            "us.anthropic.claude-sonnet-4-6",
            "global.anthropic.claude-sonnet-4-6",
            "anthropic.claude-sonnet-4-6-20260101-v1:0",
        ],
    )
    def test_every_id_format_of_a_priced_model_is_accepted(self, model):
        """The guard must not reject a model we can price because of how the id
        was written — that would make it a worse bug than the one it prevents."""
        from halo.platform.bedrock import is_priced

        assert is_priced(model)

    def test_pricing_itself_still_returns_zero_rather_than_raising(self):
        """The lookup is not the place to take a run down. The refusal happens
        at construction, where there is something useful to say."""
        from halo.platform.bedrock import estimate_usd

        assert estimate_usd("us.anthropic.claude-nonesuch-9", 1_000, 1_000) == Decimal("0.00")
