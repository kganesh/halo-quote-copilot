"""M2: draft a quote from tools, and prove every figure came from one.

The M1 drafter invented its numbers and said so. This one has a catalogue, a
supplier and a carrier behind an MCP gateway, and the only figures it may use are
ones a tool returned.

Two mechanics do the work, and neither is a prompt instruction:

- The model reports, for every money figure, the `tool_call_id` it came from.
- `verify` then checks that the cited call exists in the audit **and that the
  value actually appears in that call's result.** A model that cites a real id
  for a number the tool never returned is caught here, which is the difference
  between provenance and the appearance of provenance.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from halo.domain.catalog import DecorationMethod
from halo.domain.quote import Citation, CitationKind, DecorationCharge, Quote, QuoteLine
from halo.domain.request import QuoteRequest
from halo.platform.bedrock import ModelClient
from halo.platform.budget import BudgetExceeded, BudgetTracker
from halo.platform.gateway import ToolCall, ToolGateway
from halo.platform.identity import Principal
from halo.platform.outcome import Outcome, OutcomeStatus

AGENT_NAME = "sourcing"
MAX_TURNS = 12
MAX_NUDGES = 2

REQUIRED_TOOLS = {
    "pim_oms.get_price",
    "supplier.get_decoration_charges",
    "supplier.earliest_ship_date",
    "shipping.estimate_freight",
}
"""Tools a complete sourcing must have called.

Enforced by the loop rather than asked for in the prompt. Three live runs went
three different ways — the model back-computed a ship date from the customer's
deadline, then skipped `earliest_ship_date` altogether and cited a made-up id —
and each time the fix would have been another sentence of prompt. A sequence the
answer depends on belongs in the harness, where it either happened or it did not.
"""

SYSTEM_PROMPT = """\
You are sourcing a quote for a HALO sales representative, using the tools
provided. HALO distributes branded merchandise.

Today's date is {today:%A %d %B %Y}.

Every money figure and every date in your answer must come from a tool result.
You have no pricing, stock, capacity or freight knowledge of your own, and a
figure you did not fetch is not usable — if a tool cannot give you something,
record it in `unresolved` rather than supplying it yourself.

Work in this order:

1. `search_products` to find a SKU matching the request.
2. `get_price` for that SKU at the requested quantity.
3. `check_inventory` with the `method` argument set, so only suppliers that can
   both hold the goods and decorate them come back.
4. `get_decoration_charges` for the decoration method and colour count.
5. `earliest_ship_date` for the supplier that can actually do the run.
6. `estimate_freight` to the destination state.

`promised_ship_date` is the `ship_date` string that `earliest_ship_date` returned,
copied verbatim. Do not compute it, adjust it, or work backwards from the date the
customer asked for. If the supplier's date misses the customer's, that is a fact
for `unresolved` — not a reason to change the date.

For each figure you report, give the `tool_call_id` of the call that produced it.
Every tool result is labelled with its id. Do not guess an id, and do not reuse
one from a different figure — both are checked.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_products",
        "description": "Find catalogue SKUs matching a plain-English product description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": [
                        "outerwear",
                        "knits",
                        "headwear",
                        "drinkware",
                        "bags",
                        "tech",
                        "writing",
                    ],
                },
                "limit": {"type": "integer"},
            },
            "required": ["description"],
        },
    },
    {
        "name": "get_price",
        "description": "Unit price for a SKU at a quantity, from the quantity-break table.",
        "input_schema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}, "quantity": {"type": "integer"}},
            "required": ["sku", "quantity"],
        },
    },
    {
        "name": "check_inventory",
        "description": "Which suppliers hold enough of a SKU to cover a quantity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "quantity": {"type": "integer"},
                "color": {"type": "string"},
                "method": {
                    "type": "string",
                    "description": "decoration method; restricts to suppliers that offer it",
                },
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
    },
]

TOOL_ROUTES = {
    "search_products": "pim_oms.search_products",
    "get_price": "pim_oms.get_price",
    "check_inventory": "supplier.check_inventory",
    "get_decoration_charges": "supplier.get_decoration_charges",
    "earliest_ship_date": "supplier.earliest_ship_date",
    "estimate_freight": "shipping.estimate_freight",
}


class SourcedFigure(BaseModel):
    """A number and the call it came from. Both halves are checked."""

    value: Decimal
    tool_call_id: str = Field(pattern=r"^tc-\d{4}$")


class SourcingDecision(BaseModel):
    """What the model concluded, with a receipt for every figure."""

    sku: str
    product_name: str
    quantity: int = Field(ge=1)
    unit_price: SourcedFigure
    decoration_method: DecorationMethod
    imprint_colors: int = Field(ge=1)
    decoration_setup_fee: SourcedFigure
    decoration_run_charge_per_unit: SourcedFigure
    supplier_id: str
    supplier_name: str
    promised_ship_date: date
    ship_date_tool_call_id: str = Field(pattern=r"^tc-\d{4}$")
    freight_cost: SourcedFigure
    unresolved: list[str] = Field(default_factory=list)


def _values_under(result: Any, hints: tuple[str, ...]) -> set[str]:
    """Scalars sitting under a key whose name matches one of `hints`.

    Searching every scalar in the result was the first attempt and it is too
    loose: `estimate_freight` returns `{"freight_cost": "294.00", "zone": 2}`, so
    a fabricated freight cost of $2.00 matched the *zone number* and verified
    clean. Restricting the search to plausibly-named fields closes that, and
    makes "no field here could carry this figure" its own answer.
    """
    found: set[str] = set()
    if isinstance(result, dict):
        for key, value in result.items():
            if any(hint in key.lower() for hint in hints):
                found |= _scalars(value)
            else:
                found |= _values_under(value, hints)
    elif isinstance(result, list):
        for item in result:
            found |= _values_under(item, hints)
    return found


def _scalars(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {s for v in value.values() for s in _scalars(v)}
    if isinstance(value, list):
        return {s for item in value for s in _scalars(item)}
    return {str(value)}


def _matches(value: Decimal | date, result: Any, hints: tuple[str, ...]) -> bool:
    candidates = _values_under(result, hints)
    if not candidates:
        return False
    if isinstance(value, date):
        return value.isoformat() in candidates
    for candidate in candidates:
        try:
            if Decimal(candidate) == value:
                return True
        except (InvalidOperation, ValueError):
            continue
    return False


def verify(decision: SourcingDecision, audit: list[ToolCall]) -> list[str]:
    """Problems with the decision's provenance. Empty means every figure checks out."""
    by_id = {call.id: call for call in audit}
    problems: list[str] = []

    # Each figure names the field it could legitimately have come from. Without
    # this, any number in the result would do — see `_values_under`.
    checks: list[tuple[str, Decimal | date, str, tuple[str, ...]]] = [
        ("unit_price", decision.unit_price.value, decision.unit_price.tool_call_id, ("price",)),
        (
            "decoration_setup_fee",
            decision.decoration_setup_fee.value,
            decision.decoration_setup_fee.tool_call_id,
            ("setup",),
        ),
        (
            "decoration_run_charge_per_unit",
            decision.decoration_run_charge_per_unit.value,
            decision.decoration_run_charge_per_unit.tool_call_id,
            ("run_charge",),
        ),
        (
            "freight_cost",
            decision.freight_cost.value,
            decision.freight_cost.tool_call_id,
            ("freight", "cost"),
        ),
        (
            "promised_ship_date",
            decision.promised_ship_date,
            decision.ship_date_tool_call_id,
            ("date",),
        ),
    ]

    for field_name, value, call_id, hints in checks:
        call = by_id.get(call_id)
        if call is None:
            problems.append(f"{field_name} cites {call_id}, which is not in the audit")
        elif not call.ok:
            problems.append(f"{field_name} cites {call_id}, which failed: {call.error}")
        elif not _matches(value, call.result, hints):
            problems.append(f"{field_name}={value} does not appear in {call_id} ({call.name})")

    return problems


def _citation(field_name: str, call_id: str, audit: list[ToolCall]) -> Citation:
    call = next(c for c in audit if c.id == call_id)
    return Citation(
        kind=CitationKind.TOOL_CALL,
        ref=call_id,
        supporting_text=f"{field_name} from {call.name}({json.dumps(call.arguments, default=str)})",
    )


def assemble(decision: SourcingDecision, audit: list[ToolCall]) -> Quote:
    """Turn a verified decision into a Quote. Call `verify` first."""
    return Quote(
        request_id=f"req-{len(audit):04d}",
        account_id="acct-mwest02",
        lines=[
            QuoteLine(
                sku=decision.sku,
                description=decision.product_name,
                quantity=decision.quantity,
                unit_price=decision.unit_price.value,
                citations=[_citation("unit price", decision.unit_price.tool_call_id, audit)],
            )
        ],
        decoration=DecorationCharge(
            method=decision.decoration_method,
            colors=decision.imprint_colors,
            setup_fee=decision.decoration_setup_fee.value,
            run_charge_per_unit=decision.decoration_run_charge_per_unit.value,
            citations=[
                _citation("setup fee", decision.decoration_setup_fee.tool_call_id, audit),
                _citation(
                    "run charge", decision.decoration_run_charge_per_unit.tool_call_id, audit
                ),
            ],
        ),
        shipping_cost=decision.freight_cost.value,
        shipping_citations=[_citation("freight", decision.freight_cost.tool_call_id, audit)],
        promised_ship_date=decision.promised_ship_date,
        margin_pct=Decimal("0.0"),
    )


async def source_quote(
    request: QuoteRequest,
    *,
    principal: Principal,
    client: ModelClient,
    gateway: ToolGateway,
    tracker: BudgetTracker,
    today: date | None = None,
) -> Outcome:
    """Run the tool loop, verify what comes back, and assemble or escalate."""
    system = SYSTEM_PROMPT.format(today=today or date.today())
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": request.model_dump_json(indent=2)}
    ]

    nudges = 0
    try:
        for _ in range(MAX_TURNS):
            turn = client.converse(system=system, messages=messages, tools=TOOLS)
            messages.append({"role": "assistant", "content": turn.content})

            if turn.stop_reason != "tool_use":
                missing = REQUIRED_TOOLS - {c.name for c in gateway.audit if c.ok}
                if not missing or nudges >= MAX_NUDGES:
                    break
                nudges += 1
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have not called these yet, and the quote cannot be "
                            f"completed without them: {', '.join(sorted(missing))}. "
                            "Call them now."
                        ),
                    }
                )
                continue

            results = []
            for block in turn.tool_uses:
                route = TOOL_ROUTES.get(block.name, block.name)
                call = await gateway.call(route, dict(block.input))
                # The id is handed back with the result so the model can cite it.
                # Nothing else in the loop tells it what a tool_call_id looks like.
                payload = {
                    "tool_call_id": call.id,
                    "result" if call.ok else "error": call.result if call.ok else call.error,
                }
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(payload, default=str),
                        "is_error": not call.ok,
                    }
                )
            messages.append({"role": "user", "content": results})
        else:
            return Outcome(
                status=OutcomeStatus.ESCALATED,
                agent=AGENT_NAME,
                escalation_reason=f"gave up after {MAX_TURNS} turns without a decision",
                next_state="needs_human_sourcing",
                usage=tracker.usage,
            )

        if missing := REQUIRED_TOOLS - {c.name for c in gateway.audit if c.ok}:
            return Outcome(
                status=OutcomeStatus.ESCALATED,
                agent=AGENT_NAME,
                escalation_reason=(
                    "sourcing is incomplete — these tools were never called "
                    f"successfully: {', '.join(sorted(missing))}"
                ),
                next_state="needs_human_sourcing",
                usage=tracker.usage,
            )

        decision_result = client.parse(
            system="Report the sourcing you just completed, with a tool_call_id per figure.",
            user=_transcript(messages),
            output_format=SourcingDecision,
        )
    except BudgetExceeded as exc:
        return Outcome(
            status=OutcomeStatus.ESCALATED,
            agent=AGENT_NAME,
            escalation_reason=f"budget exhausted while sourcing: {exc}",
            next_state="await_budget_increase",
            usage=tracker.usage,
        )

    decision = decision_result.parsed
    audit = gateway.audit

    if problems := verify(decision, audit):
        return Outcome(
            status=OutcomeStatus.ESCALATED,
            agent=AGENT_NAME,
            payload=decision.model_dump(mode="json"),
            escalation_reason="figures could not be traced to the tools that supposedly "
            f"produced them: {'; '.join(problems)}",
            next_state="needs_regrounding",
            usage=tracker.usage,
        )

    if decision.unresolved:
        return Outcome(
            status=OutcomeStatus.ESCALATED,
            agent=AGENT_NAME,
            payload=decision.model_dump(mode="json"),
            evidence=[_citation("unit price", decision.unit_price.tool_call_id, audit)],
            escalation_reason="sourced, but incomplete: " + "; ".join(decision.unresolved),
            next_state="needs_human_sourcing",
            usage=tracker.usage,
        )

    quote = assemble(decision, audit)
    return Outcome(
        status=OutcomeStatus.COMPLETED,
        agent=AGENT_NAME,
        payload=quote.model_dump(mode="json"),
        evidence=quote.all_citations,
        next_state="ready_for_margin_review",
        usage=tracker.usage,
    )


def _transcript(messages: list[dict[str, Any]]) -> str:
    """A plain-text rendering of the loop, for the reporting call.

    Only tool results and assistant text are kept: the reporting call needs the
    ids and the figures, not the model's own reasoning about them.
    """
    lines: list[str] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            lines.append(f"REQUEST: {content}")
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                lines.append(f"TOOL RESULT: {block['content']}")
            elif getattr(block, "type", None) == "text":
                lines.append(f"ASSISTANT: {block.text}")
    return "\n".join(lines)
