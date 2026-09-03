"""M1's done-criteria: a credible draft, and a run that refuses to call it an answer."""

from decimal import Decimal

import pytest

from fakes import FakeModelClient, a_draft
from halo.agents.drafter import AGENT_NAME, draft_quote
from halo.domain.request import UngroundedDraft
from halo.platform.bedrock import estimate_usd
from halo.platform.budget import Budget, BudgetTracker
from halo.platform.identity import Principal, Role
from halo.platform.outcome import OutcomeStatus

REQUEST = (
    "Customer wants 500 hoodies, 3-colour front print, delivered to Chicago by Oct 15, budget $12k."
)


@pytest.fixture
def principal() -> Principal:
    return Principal(
        user_id="usr-mwest01",
        tenant_id="tnt-mwest1",
        role=Role.SELLER,
        account_ids=("acct-mwest02",),
    )


@pytest.fixture
def tracker() -> BudgetTracker:
    return BudgetTracker(
        Budget(
            wall_clock_seconds=120,
            max_tokens=40_000,
            max_tool_calls=0,
            max_usd=Decimal("0.25"),
        ),
        now=lambda: 0.0,
    )


def test_a_draft_never_completes(principal, tracker):
    """There is no citation available, so there is no legitimate completion."""
    client = FakeModelClient(tracker=tracker)
    outcome = draft_quote(REQUEST, principal=principal, client=client, tracker=tracker)

    assert outcome.status is OutcomeStatus.ESCALATED
    assert outcome.agent == AGENT_NAME
    assert outcome.next_state == "needs_grounding"


def test_the_escalation_names_what_is_missing(principal, tracker):
    client = FakeModelClient(tracker=tracker)
    outcome = draft_quote(REQUEST, principal=principal, client=client, tracker=tracker)

    reason = outcome.escalation_reason
    assert "ungrounded" in reason
    assert "4 figure(s) were assumed" in reason
    assert principal.user_id in reason


def test_no_evidence_is_fabricated_to_look_complete(principal, tracker):
    client = FakeModelClient(tracker=tracker)
    outcome = draft_quote(REQUEST, principal=principal, client=client, tracker=tracker)
    assert outcome.evidence == []


def test_the_draft_survives_a_round_trip(principal, tracker):
    """The payload has to reload as a draft — M2 picks it up from here."""
    client = FakeModelClient(tracker=tracker)
    outcome = draft_quote(REQUEST, principal=principal, client=client, tracker=tracker)

    draft = UngroundedDraft.model_validate(outcome.payload)
    assert draft.subtotal == Decimal("7900.00")
    assert draft.total == Decimal("9051.00")


def test_the_seller_sentence_reaches_the_model_unedited(principal, tracker):
    client = FakeModelClient(tracker=tracker)
    draft_quote(REQUEST, principal=principal, client=client, tracker=tracker)

    assert client.calls[0]["user"] == REQUEST
    assert client.calls[0]["output_format"] is UngroundedDraft


def test_spend_is_recorded_against_the_budget(principal, tracker):
    client = FakeModelClient(tracker=tracker)
    outcome = draft_quote(REQUEST, principal=principal, client=client, tracker=tracker)

    assert outcome.usage.input_tokens == 1_450
    assert outcome.usage.output_tokens == 820
    assert outcome.usage.usd == estimate_usd("us.anthropic.claude-sonnet-4-6", 1_450, 820)
    assert outcome.usage.usd > Decimal("0")


def test_an_exhausted_budget_escalates_rather_than_calling_the_model(principal):
    """Rule 05: the ceiling stops the run before it spends, not after."""
    spent = BudgetTracker(
        Budget(
            wall_clock_seconds=120,
            max_tokens=100,
            max_tool_calls=0,
            max_usd=Decimal("0.25"),
        ),
        now=lambda: 0.0,
    )
    spent.record_model_call(400, 400, Decimal("0.01"))

    client = FakeModelClient(tracker=spent)
    outcome = draft_quote(REQUEST, principal=principal, client=client, tracker=spent)

    assert outcome.status is OutcomeStatus.ESCALATED
    assert "budget exhausted" in outcome.escalation_reason
    assert outcome.next_state == "await_budget_increase"
    assert client.calls == [], "the model was called despite an exhausted budget"


def test_a_draft_must_name_at_least_one_assumption(principal, tracker):
    """A draft claiming it assumed nothing is the one shape that must not parse."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        a_draft(assumptions=[])


class TestTheModelIsToldWhatDayItIs:
    """A model has no clock. The first real run resolved "by Oct 15" to a date
    that had already passed."""

    def test_todays_date_reaches_the_prompt(self, principal, tracker):
        from datetime import date

        client = FakeModelClient(tracker=tracker)
        draft_quote(
            REQUEST,
            principal=principal,
            client=client,
            tracker=tracker,
            today=date(2026, 9, 3),
        )
        system = client.calls[0]["system"]
        assert "Thursday 03 September 2026" in system
        assert "never to a past one" in system

    def test_the_prompt_is_fully_rendered(self, principal, tracker):
        """An unformatted placeholder would ship a literal brace to the model."""
        client = FakeModelClient(tracker=tracker)
        draft_quote(REQUEST, principal=principal, client=client, tracker=tracker)
        assert "{today" not in client.calls[0]["system"]


class TestBedrockRates:
    """Rates come from the Bedrock offer rate card, not the first-party list."""

    def test_the_global_profile_is_cheaper_than_the_regional_one(self):
        regional = estimate_usd("us.anthropic.claude-sonnet-4-6", 1_000_000, 0)
        globally = estimate_usd("global.anthropic.claude-sonnet-4-6", 1_000_000, 0)
        assert regional == Decimal("3.30")
        assert globally < regional
        assert round(globally, 2) == Decimal("3.00")

    def test_output_tokens_cost_five_times_input(self):
        assert estimate_usd("us.anthropic.claude-sonnet-4-6", 0, 1_000_000) == Decimal("16.50")

    def test_an_unpriced_model_costs_nothing_rather_than_raising(self):
        """A budget is a safety rail; failing a run over an unknown price would
        make it a hazard."""
        assert estimate_usd("anthropic.claude-sonnet-5", 1000, 1000) == Decimal("0.00")
