"""The CLI is the M1 demo, so its output is part of the deliverable."""

from decimal import Decimal

from fakes import a_draft
from halo.cli import render
from halo.platform.budget import Usage
from halo.platform.outcome import Outcome, OutcomeStatus


def an_outcome() -> Outcome:
    draft = a_draft()
    return Outcome(
        status=OutcomeStatus.ESCALATED,
        agent="drafter",
        payload=draft.model_dump(mode="json"),
        escalation_reason="draft is ungrounded: 4 figure(s) were assumed rather than looked up",
        next_state="needs_grounding",
        usage=Usage(input_tokens=1450, output_tokens=820, usd=Decimal("0.0111")),
    )


def test_the_render_shows_the_quote_and_the_fabrication_together():
    out = render(an_outcome())
    assert "DRAFT" in out
    assert "MADE UP (4)" in out
    assert "HL-KNT-2200" in out
    assert "assumed $22.00 per screen setup" in out


def test_the_render_states_the_outcome_and_the_spend():
    out = render(an_outcome())
    assert "OUTCOME  escalated" in out
    assert "next state: needs_grounding" in out
    assert "1,450 in + 820 out tokens" in out
    assert "$0.0111" in out


def test_the_total_shown_is_the_computed_one_not_a_model_claim():
    """Money is arithmetic over the parts, never a number the model reported."""
    out = render(an_outcome())
    assert str(a_draft().total) in out


class TestSetupErrorsAreExplained:
    """Every one of these is fixed once, by an operator, and a traceback helps
    with none of them."""

    def _explain(self, error):
        from halo.cli import explain

        return explain(error, region="us-east-1", model="anthropic.claude-sonnet-5")

    def test_missing_credentials_says_how_to_supply_them(self):
        out = self._explain(RuntimeError("Could not resolve AWS credentials from session"))
        assert "No AWS credentials found" in out
        assert "AWS_PROFILE" in out

    def test_model_not_enabled_points_at_model_access(self):
        import anthropic
        import httpx2

        error = anthropic.NotFoundError(
            "not found",
            response=httpx2.Response(404, request=httpx2.Request("POST", "https://x")),
            body=None,
        )
        out = self._explain(error)
        assert "Model access" in out
        assert "us-east-1" in out

    def test_an_unrecognised_error_is_still_named(self):
        out = self._explain(ValueError("something else"))
        assert out == "ValueError: something else"


class TestEndToEnd:
    """The CLI path, driven against a fake model — no credentials, no spend."""

    def _factory(self, **_kwargs):
        from fakes import FakeModelClient

        return FakeModelClient()

    def test_a_run_prints_the_draft_and_exits_non_zero(self, capsys):
        from halo.cli import main

        code = main(["quote", "500 hoodies, 3-colour front print"], client_factory=self._factory)
        out = capsys.readouterr().out

        assert "MADE UP (4)" in out
        assert "OUTCOME  escalated" in out
        assert code == 2, "an escalation must not report success"

    def test_json_mode_emits_a_loadable_outcome(self, capsys):
        import json

        from halo.cli import main
        from halo.platform.outcome import Outcome

        main(["quote", "500 hoodies", "--json"], client_factory=self._factory)
        payload = json.loads(capsys.readouterr().out)

        outcome = Outcome.model_validate(payload)
        assert outcome.status.value == "escalated"
        assert outcome.evidence == []

    def test_a_setup_failure_exits_one_with_an_instruction(self, capsys):
        from halo.cli import main

        def broken(**_kwargs):
            raise RuntimeError("Could not resolve AWS credentials from session")

        code = main(["quote", "500 hoodies"], client_factory=broken)
        err = capsys.readouterr().err

        assert code == 1
        assert "No AWS credentials found" in err
