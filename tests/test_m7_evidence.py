"""M7: the trace, the event record, and the gates that fail a build.

The deliverable is one trace showing a complete quote decision by decision, and
it is asserted here rather than demonstrated: a trace is only worth having if the
spans that explain a decision are there every time, including on the runs that
produced nothing.

The redaction tests matter more than they look. A bucket is copied, granted to a
data team, and crawled by something nobody remembers enabling, so an event has to
be safe at rest rather than safe when read carefully.
"""

import json
from decimal import Decimal

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fakes import SpecialistModel
from halo.agents import supervisor
from halo.evals import gates
from halo.evals.atlas_golden import GOLDEN
from halo.platform import telemetry
from halo.platform.checkpoint import FileCheckpointStore, approve
from halo.platform.events import (
    REDACTED,
    SCHEMA_VERSION,
    Event,
    FileEventSink,
    NullEventSink,
    S3EventSink,
    redact,
)
from halo.platform.outcome import OutcomeStatus
from test_m6_supervisor import (
    HEALTHY_SKU,
    MANAGER,
    SELLER,
    THIN_SKU,
    a_gateway,
    a_request,
)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    telemetry.configure(exporter)
    return exporter


@pytest.fixture
def store(tmp_path) -> FileCheckpointStore:
    return FileCheckpointStore(tmp_path / "checkpoints")


async def a_run(sku: str, store, events=None):
    gateway = a_gateway()
    client = SpecialistModel(gateway, sku=sku, quantity=500)
    outcome = await supervisor.draft(
        a_request(),
        principal=SELLER,
        client=client,
        gateway=gateway,
        store=store,
        events=events,
    )
    return outcome


def kinds(exporter: InMemorySpanExporter) -> list[str]:
    return [span.attributes["halo.kind"] for span in exporter.get_finished_spans()]


def named(exporter: InMemorySpanExporter, name: str):
    return next(s for s in exporter.get_finished_spans() if s.name == name)


class TestTheTrace:
    async def test_a_complete_quote_produces_all_four_machine_span_kinds(self, spans, store):
        """State, model, tool, decision. Approval is the fifth and needs a human,
        which is the next test."""
        outcome = await a_run(HEALTHY_SKU, store)

        assert outcome.status is OutcomeStatus.COMPLETED
        assert {"state", "model", "tool", "decision"} <= set(kinds(spans))

    async def test_every_specialist_appears_as_its_own_state_span(self, spans, store):
        await a_run(HEALTHY_SKU, store)
        states = [
            s.name for s in spans.get_finished_spans() if s.attributes["halo.kind"] == "state"
        ]

        assert states == ["state.pricing", "state.supply", "state.logistics"]

    async def test_a_tool_span_carries_the_id_a_quote_will_cite(self, spans, store):
        """The join between a trace and the evidence. Without it, a span saying
        `get_price` took 40ms cannot be tied to the figure on the quote."""
        await a_run(HEALTHY_SKU, store)
        tools = [s for s in spans.get_finished_spans() if s.attributes["halo.kind"] == "tool"]

        assert tools
        assert all(s.attributes["halo.tool_call_id"].startswith("tc-") for s in tools)

    async def test_the_margin_decision_is_a_span_whether_or_not_it_gates(self, spans, store):
        await a_run(HEALTHY_SKU, store)
        decision = named(spans, "decision.margin")

        assert decision.attributes["halo.gated"] is False
        assert decision.attributes["halo.margin_pct"] == "36.7"

    async def test_a_gated_run_says_so_on_the_same_span(self, spans, store):
        await a_run(THIN_SKU, store)
        decision = named(spans, "decision.margin")

        assert decision.attributes["halo.gated"] is True
        assert decision.attributes["halo.floor_pct"] == "31.0"

    async def test_an_approval_is_the_only_span_a_human_causes(self, spans, store):
        paused = await a_run(THIN_SKU, store)
        released = approve(store.load(paused.payload["checkpoint_id"]), MANAGER)
        spans.clear()

        supervisor.resume(released)
        approval = named(spans, "approval.margin_exception")

        assert kinds(spans) == ["approval"]
        assert approval.attributes["halo.approved_by"] == MANAGER.user_id
        assert approval.attributes["halo.checkpoint_id"] == released.id

    async def test_a_run_that_produced_nothing_still_explains_itself(self, spans, store):
        """The runs worth tracing are the ones that failed. A trace with spans
        only on the happy path is a trace that is missing when it is needed."""
        from dataclasses import replace

        from halo.agents.specialists import PRICING
        from test_m6_supervisor import TINY

        original = supervisor.PRICING
        supervisor.PRICING = replace(PRICING, budget=TINY)
        try:
            outcome = await a_run(HEALTHY_SKU, store)
        finally:
            supervisor.PRICING = original

        assert outcome.status is OutcomeStatus.ESCALATED
        stopped = named(spans, "decision.pricing.stopped")
        assert stopped.attributes["halo.status"] == "escalated"
        assert stopped.attributes["halo.next_state"] == "await_budget_increase"

    async def test_the_rendered_trace_reads_as_a_sequence_of_decisions(self, spans, store):
        await a_run(HEALTHY_SKU, store)
        rendered = telemetry.render_trace(spans.get_finished_spans())

        assert "state.pricing" in rendered
        # Named by the gateway route, so a span says which system answered
        # rather than what the model called it.
        assert "tool.pim_oms.get_price" in rendered
        assert "decision.margin" in rendered
        # Nested under their state span, not flat.
        assert "  model.pricing.turn" in rendered

    def test_an_unknown_span_kind_is_refused(self):
        """A sixth kind arriving by typo is how a taxonomy stops meaning
        anything, and nothing downstream would report the mistake."""
        with pytest.raises(ValueError, match="unknown span kind"), telemetry.span("audit", "x"):
            pass


class TestTheEventRecord:
    def test_every_event_is_versioned(self):
        payload = Event(kind="margin_checked", run_id="run-1", agent="supervisor").to_json()

        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["event_id"].startswith("evt-")
        assert payload["at"]

    @pytest.mark.parametrize(
        "value",
        [
            "contact buyer@customer.com about it",
            "call 312-555-0148",
            "card 4111 1111 1111 1111 on file",
        ],
    )
    def test_pii_is_removed_when_the_event_is_built(self, value):
        """At the boundary, not on the way out. A bucket has more readers than
        the code that wrote to it."""
        assert REDACTED in redact({"note": value})["note"]

    def test_a_field_named_like_a_secret_never_travels(self):
        """The pattern catches a credential that looks like one. This catches the
        field that is one, whatever it contains."""
        cleaned = redact({"api_key": "anything at all", "nested": {"authorization": "Bearer x"}})

        assert cleaned["api_key"] == REDACTED
        assert cleaned["nested"]["authorization"] == REDACTED

    def test_redaction_reaches_every_depth(self):
        cleaned = redact({"a": [{"b": ["mail me at buyer@customer.com"]}]})
        assert REDACTED in cleaned["a"][0]["b"][0]

    def test_the_guardrail_and_the_record_share_one_pattern_list(self):
        """Two lists would drift, and the drift would be invisible: the guardrail
        is exercised by twenty red-team notes on every build, and a redaction
        rule nobody tests is found by an auditor."""
        from halo.platform import events, guardrails

        assert events.PII_PATTERNS is guardrails.PII_PATTERNS

    def test_a_file_sink_writes_one_json_object_per_line(self, tmp_path):
        sink = FileEventSink(tmp_path / "events.jsonl")
        sink.emit(Event(kind="margin_checked", run_id="run-1", agent="supervisor"))
        sink.emit(Event(kind="approval_granted", run_id="run-1", agent="supervisor"))

        lines = (tmp_path / "events.jsonl").read_text().splitlines()
        assert [json.loads(line)["kind"] for line in lines] == [
            "margin_checked",
            "approval_granted",
        ]

    def test_a_sink_that_cannot_write_does_not_fail_the_quote(self, tmp_path):
        """Losing an event is a small problem. Failing a run because one could
        not be written is a larger one."""
        unwritable = tmp_path / "events.jsonl"
        unwritable.mkdir()

        FileEventSink(unwritable).emit(Event(kind="x", run_id="r", agent="a"))

    def test_the_s3_key_is_partitioned_by_kind_and_day(self):
        """The first question asked of this bucket is always "what happened on
        the day of the complaint", and a partition answers it without scanning
        the year."""
        sink = S3EventSink("halo-events", client=object())
        key = sink.key_for(
            {"at": "2026-09-05T10:00:00+00:00", "kind": "approval_granted", "event_id": "evt-1"}
        )

        assert key == "events/kind=approval_granted/date=2026-09-05/evt-1.json"

    def test_an_s3_failure_is_swallowed(self):
        class Broken:
            def put_object(self, **kwargs):
                raise RuntimeError("no such bucket")

        S3EventSink("halo-events", client=Broken()).emit(Event(kind="x", run_id="r", agent="a"))

    async def test_a_run_records_the_margin_check_and_the_approval(self, tmp_path, store):
        sink = FileEventSink(tmp_path / "events.jsonl")
        paused = await a_run(THIN_SKU, store, events=sink)
        released = approve(store.load(paused.payload["checkpoint_id"]), MANAGER)
        supervisor.resume(released, events=sink)

        events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
        kinds_seen = [event["kind"] for event in events]

        assert kinds_seen == ["margin_checked", "approval_requested", "approval_granted"]
        assert events[0]["user_id"] == SELLER.user_id
        assert events[0]["tenant_id"] == SELLER.tenant_id
        assert events[2]["attributes"]["raised_by"] == SELLER.user_id

    async def test_the_default_sink_writes_nothing(self, store):
        """An uninstrumented run costs nothing, so nothing has to be configured
        for the tests or for a first clone."""
        outcome = await a_run(HEALTHY_SKU, store, events=NullEventSink())
        assert outcome.status is OutcomeStatus.COMPLETED


class TestTheGates:
    """M7's done-when: a grounding regression fails the build."""

    async def test_all_gates_pass_on_the_corpus_as_it_stands(self):
        result = await gates.run_all()
        assert gates.passed(result), gates.report(result)

    def test_the_gates_run_on_the_real_corpus_not_a_fixture(self):
        chunks = gates.corpus()
        assert len(chunks) > 50
        assert any(chunk.id.startswith("atl-screen-print-standards") for chunk in chunks)

    def test_a_fact_that_left_its_chunk_fails_the_fixture_gate(self):
        """The quiet rot: a document is reworded, the golden set still names the
        old chunk, and nothing fails until a live eval reads 17/20 as a model
        regression."""
        from halo.evals.atlas_golden import GoldenQuestion

        broken = GoldenQuestion(
            "How many colours?",
            "atl-screen-print-standards#colour-limits",
            "seventeen spot colours",
        )
        results = gates.check_fixtures([broken], gates.corpus())

        assert not results[0].passed
        assert "no longer in this chunk" in results[0].detail

    def test_a_question_whose_chunk_became_unfindable_fails_the_retrieval_gate(self):
        from halo.evals.atlas_golden import GoldenQuestion

        unfindable = GoldenQuestion(
            "What is the airspeed velocity of an unladen swallow?",
            "atl-screen-print-standards#colour-limits",
            "six spot colours",
        )
        results = gates.check_retrieval([unfindable], gates.corpus())

        assert not results[0].passed
        assert "not in the top" in results[0].detail

    def test_the_grounding_gate_catches_a_check_that_stopped_checking(self, monkeypatch):
        """A weakened verbatim check makes a live eval score *better*, which is
        why this one cannot be left to the live run."""
        monkeypatch.setattr("halo.agents.advisor.verify", lambda answer, retrieved: [])
        monkeypatch.setattr("halo.evals.gates.verify", lambda answer, retrieved: [])

        result = gates.check_grounding_rejects_a_paraphrase(gates.corpus())

        assert not result.passed
        assert "paraphrase was accepted" in result.detail

    def test_the_golden_set_is_still_twenty_questions(self):
        assert len(GOLDEN) == 20

    def test_the_command_exits_non_zero_when_a_gate_fails(self, monkeypatch, capsys):
        from halo import cli

        async def failing():
            return {"retrieval": [gates.GateResult("q", False, "gone")]}

        monkeypatch.setattr("halo.evals.gates.run_all", failing)
        assert cli.main(["gate"]) == 2
        assert "FAIL" in capsys.readouterr().out


class TestSpansCarryNoContent:
    """Attributes are ids, counts and outcomes. A trace goes to a third-party
    backend; the corpus and the customer's words do not."""

    async def test_no_span_attribute_contains_the_supplier_or_customer_text(self, spans, store):
        await a_run(HEALTHY_SKU, store)

        for span in spans.get_finished_spans():
            for key, value in (span.attributes or {}).items():
                assert "@" not in str(value), f"{span.name}.{key}"
                assert len(str(value)) < 120, f"{span.name}.{key} looks like content"

    async def test_an_escalation_reason_is_not_copied_onto_a_span(self, spans, store):
        """It is written by a model or built from a tool error, which makes it
        the one field on an Outcome that can carry unvetted text."""
        from dataclasses import replace

        from halo.agents.specialists import PRICING
        from test_m6_supervisor import TINY

        original = supervisor.PRICING
        supervisor.PRICING = replace(PRICING, budget=TINY)
        try:
            outcome = await a_run(HEALTHY_SKU, store)
        finally:
            supervisor.PRICING = original

        reason = outcome.escalation_reason
        assert reason
        for span in spans.get_finished_spans():
            assert reason not in str(dict(span.attributes or {}))


class TestUsageOnSpans:
    async def test_a_model_span_carries_what_the_call_cost(self, spans, store):
        await a_run(HEALTHY_SKU, store)
        model_spans = [
            s for s in spans.get_finished_spans() if s.attributes["halo.kind"] == "model"
        ]

        assert model_spans
        for span in model_spans:
            assert span.attributes["halo.input_tokens"] > 0
            assert isinstance(span.attributes["halo.usd"], float)

    def test_usage_lands_as_four_separate_categories(self):
        from halo.platform.budget import Usage

        exporter = InMemorySpanExporter()
        telemetry.configure(exporter)
        usage = Usage(
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=3,
            cache_write_tokens=2,
            usd=Decimal("0.01"),
        )

        with telemetry.span(telemetry.MODEL, "x") as span:
            telemetry.record_usage(span, usage)

        attributes = exporter.get_finished_spans()[0].attributes
        assert attributes["halo.cache_read_tokens"] == 3
        assert attributes["halo.cache_write_tokens"] == 2


class TestTheGateIsRunnableOnACleanCheckout:
    """CI checks out a repo with no `data/seed` in it — the generator is the
    source of truth and the corpus is not committed. This was red from M4 to M8
    because the workflow had no seed step, and the failure arrived as a
    traceback from inside a lambda rather than as the setup problem it was."""

    def test_a_missing_corpus_reports_itself_rather_than_crashing(self, monkeypatch):
        from halo.mcp_servers import store

        def missing(name, seed_dir=None):
            raise store.CorpusMissing(store.SEED_DIR / f"{name}.json")

        monkeypatch.setattr(store, "_table", missing)
        result = gates.check_corpus()

        assert result is not None
        assert not result.passed
        assert "make seed" in result.detail

    def test_the_workflow_seeds_before_it_tests(self):
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
        body = workflow.read_text()

        assert "halo.seed.generate" in body
        assert body.index("halo.seed.generate") < body.index("pytest -q")
        assert body.index("halo.seed.generate") < body.index("halo gate")
