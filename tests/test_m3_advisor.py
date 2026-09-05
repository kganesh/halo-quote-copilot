"""M3's mechanism: a quote is checked against the chunk, character for character.

The negative cases are the valuable ones here. Scoring 20/20 on the golden set
proves the working path. What proves the check is real is that a paraphrase is
rejected, because a paraphrase is what a model produces by default.
"""

from decimal import Decimal

import pytest

from fakes import FakeModelClient
from halo.agents.advisor import (
    Finding,
    PolicyAnswer,
    answer_policy_question,
    verify,
)
from halo.platform.budget import Budget, BudgetTracker
from halo.platform.identity import Principal, Role
from halo.platform.outcome import OutcomeStatus
from halo.rag.chunk import Chunk
from halo.rag.retrieve import ScoredChunk

CHUNK_TEXT = (
    "A maximum of six spot colours may be printed in a single imprint location.\n"
    "Each colour requires its own screen and its own setup."
)


def a_hit(chunk_id: str = "atl-screen-print-standards#colour-limits") -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(
            id=chunk_id,
            doc_id="atl-screen-print-standards",
            doc_title="Screen Print Standards",
            heading="Colour limits",
            text=CHUNK_TEXT,
            ordinal=0,
        ),
        score=0.03,
        vector_rank=1,
        lexical_rank=1,
    )


def an_answer(**overrides) -> PolicyAnswer:
    return PolicyAnswer(
        **{
            "answer": "Six spot colours maximum in one location.",
            "findings": [
                Finding(
                    claim="Six colours is the maximum per location.",
                    chunk_id="atl-screen-print-standards#colour-limits",
                    quote=(
                        "A maximum of six spot colours may be printed in a single imprint location."
                    ),
                )
            ],
            **overrides,
        }
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
            wall_clock_seconds=60,
            max_tokens=40_000,
            max_tool_calls=0,
            max_usd=Decimal("0.25"),
        ),
        now=lambda: 0.0,
    )


class TestVerify:
    def test_a_verbatim_quote_passes(self):
        assert verify(an_answer(), [a_hit()]) == []

    def test_a_paraphrase_is_rejected(self):
        """This is the failure the check exists for. Every word is true, and the
        chunk supports it. But it is not the text the chunk contains."""
        answer = an_answer(
            findings=[
                Finding(
                    claim="Six colours is the maximum.",
                    chunk_id="atl-screen-print-standards#colour-limits",
                    quote="You may print up to six spot colours in one location.",
                )
            ]
        )
        problems = verify(answer, [a_hit()])
        assert len(problems) == 1
        assert "is not in it" in problems[0]

    def test_a_quote_from_a_chunk_that_was_not_supplied_is_rejected(self):
        answer = an_answer(
            findings=[
                Finding(
                    claim="Rush costs 25% to 40%.",
                    chunk_id="atl-rush-policy#surcharge",
                    quote="Rush carries a surcharge of 25% to 40%.",
                )
            ]
        )
        assert "was not among the excerpts supplied" in verify(answer, [a_hit()])[0]

    def test_a_quote_spanning_a_line_break_still_matches(self):
        """Whitespace is normalised. Nothing else is, deliberately."""
        answer = an_answer(
            findings=[
                Finding(
                    claim="Each colour needs its own screen.",
                    chunk_id="atl-screen-print-standards#colour-limits",
                    quote="imprint location. Each colour requires its own screen",
                )
            ]
        )
        assert verify(answer, [a_hit()]) == []

    def test_case_is_not_normalised(self):
        """Lowercasing would start accepting quotes that are close but not
        exact."""
        answer = an_answer(
            findings=[
                Finding(
                    claim="Six colours maximum.",
                    chunk_id="atl-screen-print-standards#colour-limits",
                    quote="a maximum of SIX SPOT COLOURS may be printed",
                )
            ]
        )
        assert verify(answer, [a_hit()]) != []

    def test_every_bad_finding_is_reported(self):
        answer = an_answer(
            findings=[
                Finding(claim="a", chunk_id="atl-x#y", quote="not here"),
                Finding(
                    claim="b",
                    chunk_id="atl-screen-print-standards#colour-limits",
                    quote="also not here",
                ),
            ]
        )
        assert len(verify(answer, [a_hit()])) == 2


class TestAnswerFlow:
    def _client(self, answer: PolicyAnswer, tracker) -> FakeModelClient:
        return FakeModelClient(parsed=answer, tracker=tracker)

    class _Retriever:
        def __init__(self, hits):
            self._hits = hits

        def search(self, query, limit=6, pool=15):
            return self._hits

    def test_a_grounded_answer_completes_with_citations(self, principal, tracker):
        outcome, _ = answer_policy_question(
            "How many colours?",
            principal=principal,
            client=self._client(an_answer(), tracker),
            retriever=self._Retriever([a_hit()]),
            tracker=tracker,
        )

        assert outcome.status is OutcomeStatus.COMPLETED
        assert len(outcome.evidence) == 1
        assert outcome.evidence[0].ref == "atl-screen-print-standards#colour-limits"
        assert outcome.evidence[0].kind.value == "chunk"

    def test_an_unverifiable_answer_escalates(self, principal, tracker):
        answer = an_answer(
            findings=[
                Finding(
                    claim="x",
                    chunk_id="atl-screen-print-standards#colour-limits",
                    quote="a sentence the chunk does not contain",
                )
            ]
        )
        outcome, _ = answer_policy_question(
            "How many colours?",
            principal=principal,
            client=self._client(answer, tracker),
            retriever=self._Retriever([a_hit()]),
            tracker=tracker,
        )

        assert outcome.status is OutcomeStatus.ESCALATED
        assert outcome.next_state == "needs_regrounding"

    def test_an_answer_with_no_findings_escalates_rather_than_completing(self, principal, tracker):
        """A fluent answer that cites nothing is the M1 failure in a new
        form."""
        answer = an_answer(findings=[], unsupported=["nothing covers cancellation fees"])
        outcome, _ = answer_policy_question(
            "What is the cancellation fee?",
            principal=principal,
            client=self._client(answer, tracker),
            retriever=self._Retriever([a_hit()]),
            tracker=tracker,
        )

        assert outcome.status is OutcomeStatus.ESCALATED
        assert "does not cover this question" in outcome.escalation_reason
        assert "cancellation fees" in outcome.escalation_reason

    def test_an_empty_corpus_escalates_without_calling_the_model(self, principal, tracker):
        client = self._client(an_answer(), tracker)
        outcome, _ = answer_policy_question(
            "Anything?",
            principal=principal,
            client=client,
            retriever=self._Retriever([]),
            tracker=tracker,
        )

        assert outcome.status is OutcomeStatus.ESCALATED
        assert client.calls == [], "the model was asked about an empty corpus"


class TestTheGoldenRunner:
    """`halo eval` is the one command no offline test could run end to end — it
    needs Bedrock and the Atlas index — so nothing checked that the CLI's call
    matched this function's signature. It did not, from M4 until a live run
    failed on it: adding the guardrail flag to `ask` also rewrote the call to
    `run_golden`, which had no such parameter.

    These do not need a network. They check the seam that broke.
    """

    class StubRetriever:
        def __init__(self, hits):
            self._hits = hits

        def search(self, query, limit=5, pool=15):
            return self._hits

    def a_run(self, guardrail=None):
        from halo.evals.atlas_golden import GoldenQuestion
        from halo.rag.evaluate import run_golden

        return run_golden(
            [
                GoldenQuestion(
                    "What's the most colours we can screen print in one spot?",
                    "atl-screen-print-standards#colour-limits",
                    "six spot colours",
                )
            ],
            principal=Principal(
                user_id="usr-mwes01",
                tenant_id="tnt-mwest1",
                role=Role.SELLER,
                account_ids=("acct-mwes02",),
            ),
            client=FakeModelClient(an_answer()),
            retriever=self.StubRetriever([a_hit()]),
            tracker=BudgetTracker(
                Budget(
                    wall_clock_seconds=60,
                    max_tokens=100_000,
                    max_tool_calls=0,
                    max_usd=Decimal("1.00"),
                )
            ),
            guardrail=guardrail,
        )

    def test_a_grounded_answer_passes_all_three_rates(self):
        result = self.a_run()[0]

        assert result.retrieved and result.cited and result.fact_in_quote
        assert result.passed

    def test_it_accepts_the_guardrail_the_cli_passes_it(self):
        """The exact signature mismatch that broke `halo eval` for four
        milestones. `ask` runs with a guardrail, so an eval that could not take
        one would measure a path that does not ship."""
        from halo.platform.guardrails import LocalGuardrail

        assert self.a_run(guardrail=LocalGuardrail())[0].passed

    def test_the_cli_calls_it_with_arguments_it_accepts(self):
        """Cheap insurance against the next one. Every keyword the CLI passes
        has to exist here, and no live call is needed to know that."""
        import ast
        import inspect
        from pathlib import Path

        from halo.rag.evaluate import run_golden

        source = Path(inspect.getfile(run_golden)).parent.parent / "cli.py"
        tree = ast.parse(source.read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_golden"
        ]

        assert calls, "the CLI no longer calls run_golden"
        accepted = set(inspect.signature(run_golden).parameters)
        for call in calls:
            passed = {keyword.arg for keyword in call.keywords if keyword.arg}
            assert passed <= accepted, f"cli passes {passed - accepted}, which run_golden rejects"
