"""M7: five kinds of span, and nothing in them a customer would mind reading.

A quote is now produced by a supervisor, three tool loops, a retrieval agent, a
guardrail and sometimes a human. When one comes out wrong the question is never
"did it fail" — it is "which of those decided what". A trace answers that; a log
line per component does not, because the thing you need is the shape.

Five kinds, and the list is deliberately short:

  state       one step of the flow: a specialist, a retrieval, an assembly
  model       one call to a model, with what it cost
  tool        one call through the gateway, with the id a quote will cite
  decision    a choice the harness made: verified, blocked, refused, gated
  approval    a human opening the gate, and the checkpoint it released

`decision` is the one that earns its place. Model and tool spans say what
happened; a decision span says what the system concluded from it, which is the
part nobody can reconstruct afterwards from the other two.

**Attributes are ids, counts and outcomes.** No customer text, no prompt, no
retrieved excerpt, no supplier note. A trace is exported to a third-party backend
and read by whoever has the console; it is the wrong place for the corpus. The
event envelope in `events.py` is where content goes, redacted, into a bucket we
control. Keeping that line here rather than trusting each call site is why
`span()` takes attributes and not a free-text message.

Tracing is off unless `configure()` is called. Uninstrumented, `span()` is a
no-op context manager: the OpenTelemetry API returns a non-recording span when no
provider is set, so the library is not a runtime dependency of every test.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

SERVICE_NAME = "halo-quote-copilot"
SCOPE = "halo"

STATE = "state"
MODEL = "model"
TOOL = "tool"
DECISION = "decision"
APPROVAL = "approval"

KINDS = (STATE, MODEL, TOOL, DECISION, APPROVAL)


_PROVIDER: Any = None


def configure(exporter: Any = None, *, service: str = SERVICE_NAME) -> Any:
    """Install a tracer provider, or add an exporter to the one already there.

    `exporter` is a span exporter: an OTLP one in a deployment, an in-memory one
    in a test, a console one when you want to read a trace on the terminal. The
    choice is the caller's because it is a deployment decision, not a library
    one.

    The second call does not replace the first. OpenTelemetry refuses to swap a
    tracer provider once one is set — it logs a warning and keeps the original,
    which means a second `configure` would appear to work and then export
    nothing. Adding a processor to the existing provider is what the API
    actually supports, and it is also the right shape in a deployment that wants
    spans in two places.
    """
    global _PROVIDER
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    if _PROVIDER is None:
        _PROVIDER = TracerProvider(resource=Resource.create({"service.name": service}))
        trace.set_tracer_provider(_PROVIDER)
    if exporter is not None:
        _PROVIDER.add_span_processor(SimpleSpanProcessor(exporter))
    return _PROVIDER


@contextmanager
def span(kind: str, name: str, **attributes: Any) -> Iterator[Span]:
    """One span of one of the five kinds.

    The kind is an attribute rather than a name prefix so that a backend can
    group by it without parsing strings, and it is validated because a sixth
    kind appearing by typo is how a taxonomy stops meaning anything.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown span kind {kind!r}; expected one of {', '.join(KINDS)}")

    tracer = trace.get_tracer(SCOPE)
    with tracer.start_as_current_span(f"{kind}.{name}") as current:
        current.set_attribute("halo.kind", kind)
        for key, value in _clean(attributes).items():
            current.set_attribute(f"halo.{key}", value)
        try:
            yield current
        except Exception as error:
            # Recorded, then re-raised. A span that swallowed the exception would
            # make the trace disagree with what actually happened.
            current.set_status(Status(StatusCode.ERROR, type(error).__name__))
            current.record_exception(error)
            raise


def record_usage(current: Span, usage: Any) -> None:
    """Put what a step spent on its span, in the four billable categories."""
    current.set_attribute("halo.input_tokens", usage.input_tokens)
    current.set_attribute("halo.output_tokens", usage.output_tokens)
    current.set_attribute("halo.cache_read_tokens", usage.cache_read_tokens)
    current.set_attribute("halo.cache_write_tokens", usage.cache_write_tokens)
    current.set_attribute("halo.tool_calls", usage.tool_calls)
    current.set_attribute("halo.usd", float(usage.usd))


def record_outcome(current: Span, outcome: Any) -> None:
    """The decision an agent reached, as attributes rather than prose.

    The escalation reason is deliberately not copied here. It is written by a
    model or built from a tool error, which makes it the one field on an Outcome
    that can carry text nobody vetted for a trace backend.
    """
    current.set_attribute("halo.status", str(outcome.status))
    current.set_attribute("halo.agent", outcome.agent)
    if outcome.next_state:
        current.set_attribute("halo.next_state", outcome.next_state)
    current.set_attribute("halo.evidence_count", len(outcome.evidence))
    record_usage(current, outcome.usage)


def _clean(attributes: dict[str, Any]) -> dict[str, Any]:
    """Drop `None`s and coerce what OpenTelemetry will not take.

    OTel accepts str, bool, int, float and sequences of those. A Decimal or a
    date reaching `set_attribute` is dropped with a warning and the attribute is
    silently missing from the trace, which is worse than converting it here.
    """
    cleaned: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        cleaned[key] = value if isinstance(value, str | bool | int | float) else str(value)
    return cleaned


def render_trace(spans: Any) -> str:
    """The deliverable: one quote, decision by decision, on a terminal.

    Spans arrive flat with parent ids, so the tree is rebuilt here. Ordering is
    by start time within a parent, which is the order the run happened in — a
    backend sorts for you and a list does not.
    """
    by_parent: dict[Any, list[Any]] = {}
    for span in sorted(spans, key=lambda s: s.start_time):
        parent = span.parent.span_id if span.parent else None
        by_parent.setdefault(parent, []).append(span)

    lines: list[str] = []

    def walk(parent: Any, depth: int) -> None:
        for span in by_parent.get(parent, []):
            millis = (span.end_time - span.start_time) / 1_000_000
            detail = _summarise(dict(span.attributes or {}))
            lines.append(f"{'  ' * depth}{span.name:<34} {millis:7.1f}ms  {detail}")
            walk(span.context.span_id, depth + 1)

    walk(None, 0)
    return "\n".join(lines)


def _summarise(attributes: dict[str, Any]) -> str:
    """The few attributes worth seeing on one line of a terminal."""
    interesting = (
        "halo.status",
        "halo.tool_call_id",
        "halo.ok",
        "halo.stop_reason",
        "halo.margin_pct",
        "halo.gated",
        "halo.usd",
        "halo.approved_by",
        "halo.error",
    )
    shown = [
        f"{key.removeprefix('halo.')}={attributes[key]}" for key in interesting if key in attributes
    ]
    return " ".join(shown)
