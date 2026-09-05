"""M6: one supervisor, four bounded specialists, and a gate a human opens.

The supervisor is not a model. It is code that delegates, checks, and assembles.
That is deliberate: everything it does here is a decision with a right answer —
which specialist to run, whether the margin cleared the floor, whether the
evidence is complete — and none of it improves for being asked in English.
Prompting for it would add a turn, a cost, and a way to be wrong.

What the model does is the four jobs underneath, each inside its own budget.

The margin gate is the milestone's deliverable. A quote under the floor stops,
checkpoints its evidence, notifies, and returns `escalated`. `escalated` is the
right status and not a compromise: outcome.py defines it as work that is correct
but needs a human to decide, which is exactly what a margin exception is. The
`next_state` says which queue it lands in.

Resuming is the part worth reading closely. `resume` takes a checkpoint and
assembles the quote from the reports already in it. It has no client and no
gateway parameter, so it cannot call a model or a tool even by accident. That is
rule 06 made structural rather than promised.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from halo.agents.advisor import answer_policy_question
from halo.agents.loop import SpecialistRun, run_specialist
from halo.agents.specialists import (
    LOGISTICS,
    POLICY_BUDGET,
    PRICING,
    SUPPLY,
    LogisticsReport,
    PricingReport,
    SupplyReport,
)
from halo.domain.catalog import DecorationMethod
from halo.domain.quote import Citation, CitationKind, DecorationCharge, Quote, QuoteLine
from halo.domain.request import QuoteRequest
from halo.platform.bedrock import ModelClient
from halo.platform.budget import BudgetTracker, Usage
from halo.platform.checkpoint import (
    Checkpoint,
    CheckpointStore,
    FileCheckpointStore,
    new_id,
    now,
)
from halo.platform.gateway import ToolCall, ToolGateway
from halo.platform.guardrails import Guardrail
from halo.platform.identity import Principal
from halo.platform.outcome import Outcome, OutcomeStatus

AGENT_NAME = "supervisor"


def notify(message: str) -> None:
    """Where an approval request goes.

    A print today, an SNS topic at M8. It is a named function rather than a call
    to `print` inside the flow so that the notification is a thing that can be
    replaced, and so a test can see that one was sent."""
    print(f"[approval] {message}")


def _usage_of(runs: list[SpecialistRun]) -> Usage:
    """What the whole request cost, summed from what each specialist spent.

    The specialists hold separate budgets on purpose, so there is no single
    tracker to read. This is the only place the numbers come back together, and
    it is what the ledger records for the run."""
    total = Usage()
    for run in runs:
        used = run.outcome.usage
        total.input_tokens += used.input_tokens
        total.output_tokens += used.output_tokens
        total.cache_read_tokens += used.cache_read_tokens
        total.cache_write_tokens += used.cache_write_tokens
        total.tool_calls += used.tool_calls
        total.usd += used.usd
    return total


def margin_pct(unit_price: Decimal, base_cost: Decimal) -> Decimal:
    """Gross margin on the goods, as a percentage of the sell price.

    Freight and decoration are excluded, per `atl-margin-floors`. The tools serve
    both numbers, so nothing here is remembered: this function only divides."""
    if unit_price <= 0:
        return Decimal("0.0")
    return (((unit_price - base_cost) / unit_price) * 100).quantize(Decimal("0.1"))


async def draft(
    request: QuoteRequest,
    *,
    principal: Principal,
    client: ModelClient,
    gateway: ToolGateway,
    guardrail: Guardrail | None = None,
    retriever: Any = None,
    store: CheckpointStore | None = None,
    today: date | None = None,
) -> Outcome:
    """Delegate, check the margin, and either finish or stop for approval."""
    runs: list[SpecialistRun] = []

    pricing_run, pricing = await run_specialist(
        PRICING,
        _brief(request),
        principal=principal,
        client=client,
        gateway=gateway,
        guardrail=guardrail,
        today=today,
    )
    runs.append(pricing_run)
    if not pricing_run.ok:
        return _stopped(pricing_run, runs)

    supply_run, supply = await run_specialist(
        SUPPLY,
        f"{_brief(request)}\nSKU: {pricing.sku}\nUnits: {pricing.quantity}",
        principal=principal,
        client=client,
        gateway=gateway,
        guardrail=guardrail,
        today=today,
    )
    runs.append(supply_run)
    if not supply_run.ok:
        return _stopped(supply_run, runs)

    logistics_run, logistics = await run_specialist(
        LOGISTICS,
        f"{_brief(request)}\nUnits: {pricing.quantity}",
        principal=principal,
        client=client,
        gateway=gateway,
        guardrail=guardrail,
        today=today,
    )
    runs.append(logistics_run)
    if not logistics_run.ok:
        return _stopped(logistics_run, runs)

    # Policy is the M3 advisor, kept as it is rather than reimplemented: it
    # already checks a claim against the chunk it cites, character for character.
    policy_evidence: list[Citation] = []
    policy_notes: list[str] = []
    if retriever is not None:
        tracker = BudgetTracker(POLICY_BUDGET, owner="policy")
        policy_outcome, _ = answer_policy_question(
            _policy_question(request),
            principal=principal,
            client=client,
            retriever=retriever,
            tracker=tracker,
            guardrail=guardrail,
        )
        runs.append(SpecialistRun("policy", policy_outcome))
        if policy_outcome.status is OutcomeStatus.COMPLETED:
            policy_evidence = list(policy_outcome.evidence)
            policy_notes = [policy_outcome.payload["answer"]]
        else:
            # A policy question that could not be answered is not a reason to
            # withhold a priced quote. It is a reason to say so on it.
            policy_notes = [f"policy unresolved: {policy_outcome.escalation_reason}"]

    calls = [call for run in runs for call in run.calls]
    achieved = margin_pct(pricing.unit_price.value, pricing.base_cost.value)
    floor = pricing.margin_floor_pct.value

    if achieved < floor:
        checkpoint = Checkpoint(
            id=new_id(),
            created_at=now(),
            reason=f"margin {achieved}% is below the {floor}% floor for {pricing.category}",
            request=request.model_dump(mode="json"),
            principal=principal.model_dump(mode="json"),
            reports={
                "pricing": pricing.model_dump(mode="json"),
                "supply": supply.model_dump(mode="json"),
                "logistics": logistics.model_dump(mode="json"),
                "policy": {
                    "notes": policy_notes,
                    "evidence": [c.model_dump() for c in policy_evidence],
                },
            },
            calls=[_call_json(call) for call in calls],
            margin_pct=str(achieved),
            floor_pct=str(floor),
            usage=_usage_of(runs).model_dump(mode="json"),
        )
        (store or FileCheckpointStore()).save(checkpoint)
        notify(
            f"{checkpoint.id}: {checkpoint.reason} — approve with `halo approve {checkpoint.id}`"
        )

        return Outcome(
            status=OutcomeStatus.ESCALATED,
            agent=AGENT_NAME,
            payload={
                "checkpoint_id": checkpoint.id,
                "margin_pct": str(achieved),
                "floor_pct": str(floor),
            },
            evidence=policy_evidence,
            escalation_reason=checkpoint.reason,
            next_state="awaiting_margin_approval",
            usage=_usage_of(runs),
        )

    quote = assemble(request, pricing, supply, logistics, calls, margin=achieved)
    return _completed(quote, policy_notes, policy_evidence, _usage_of(runs))


def resume(checkpoint: Checkpoint) -> Outcome:
    """Finish an approved run from its checkpoint. No model, no tools.

    Every figure here was verified when it was gathered, and the citations
    resolve against the calls stored beside them. Re-fetching would produce a
    different quote from the one that was approved.
    """
    if checkpoint.open:
        return Outcome(
            status=OutcomeStatus.ESCALATED,
            agent=AGENT_NAME,
            escalation_reason=f"{checkpoint.id} has not been approved",
            next_state="awaiting_margin_approval",
            usage=Usage(**checkpoint.usage),
        )

    request = QuoteRequest.model_validate(checkpoint.request)
    pricing = PricingReport.model_validate(checkpoint.reports["pricing"])
    supply = SupplyReport.model_validate(checkpoint.reports["supply"])
    logistics = LogisticsReport.model_validate(checkpoint.reports["logistics"])
    calls = [ToolCall(**call) for call in checkpoint.calls]
    policy = checkpoint.reports.get("policy", {})

    quote = assemble(
        request,
        pricing,
        supply,
        logistics,
        calls,
        margin=Decimal(checkpoint.margin_pct),
        account_id=checkpoint.owner().account_ids[0],
    )
    notes = list(policy.get("notes", [])) + [
        f"margin exception {checkpoint.id} approved by {checkpoint.approved_by} "
        f"at {checkpoint.approved_at}"
    ]
    evidence = [Citation.model_validate(c) for c in policy.get("evidence", [])]
    return _completed(quote, notes, evidence, Usage(**checkpoint.usage), approved=checkpoint)


def assemble(
    request: QuoteRequest,
    pricing: PricingReport,
    supply: SupplyReport,
    logistics: LogisticsReport,
    calls: list[ToolCall],
    *,
    margin: Decimal,
    account_id: str | None = None,
) -> Quote:
    """Four reports into one quote, with a citation on every figure."""
    by_id = {call.id: call for call in calls}

    def cite(label: str, call_id: str) -> Citation:
        call = by_id.get(call_id)
        source = call.name if call else "unknown tool"
        return Citation(
            kind=CitationKind.TOOL_CALL,
            ref=call_id,
            supporting_text=f"{label} from {source}",
        )

    return Quote(
        request_id=f"req-{len(calls):04d}",
        account_id=account_id or request.account_id or "acct-unknown",
        lines=[
            QuoteLine(
                sku=pricing.sku,
                description=pricing.product_name,
                quantity=pricing.quantity,
                unit_price=pricing.unit_price.value,
                citations=[cite("unit price", pricing.unit_price.tool_call_id)],
            )
        ],
        decoration=DecorationCharge(
            method=request.decoration_method or DecorationMethod.SCREEN_PRINT,
            colors=request.imprint_colors or 1,
            setup_fee=supply.decoration_setup_fee.value,
            run_charge_per_unit=supply.decoration_run_charge_per_unit.value,
            citations=[
                cite("setup fee", supply.decoration_setup_fee.tool_call_id),
                cite("run charge", supply.decoration_run_charge_per_unit.tool_call_id),
            ],
        ),
        shipping_cost=logistics.freight_cost.value,
        shipping_citations=[cite("freight", logistics.freight_cost.tool_call_id)],
        promised_ship_date=supply.promised_ship_date,
        margin_pct=margin,
    )


def delivery_estimate(supply: SupplyReport, logistics: LogisticsReport) -> date:
    """Ship date plus transit. Neither specialist may compute this alone."""
    return supply.promised_ship_date + timedelta(days=int(logistics.transit_days.value))


def _completed(
    quote: Quote,
    notes: list[str],
    evidence: list[Citation],
    usage: Usage,
    approved: Checkpoint | None = None,
) -> Outcome:
    payload = quote.model_dump(mode="json")
    payload["notes"] = notes
    payload["total"] = str(quote.total)
    if approved is not None:
        payload["approved_by"] = approved.approved_by
        payload["checkpoint_id"] = approved.id
    return Outcome(
        status=OutcomeStatus.COMPLETED,
        agent=AGENT_NAME,
        payload=payload,
        evidence=quote.all_citations + evidence,
        next_state="ready_to_send",
        usage=usage,
    )


def _stopped(run: SpecialistRun, runs: list[SpecialistRun]) -> Outcome:
    """A specialist could not finish, so neither can the quote.

    The reason is carried up unchanged, still naming the specialist, and no
    partial quote goes with it. Half a quote reads exactly like a whole one.
    """
    return Outcome(
        status=run.outcome.status,
        agent=AGENT_NAME,
        escalation_reason=run.outcome.escalation_reason,
        next_state=run.outcome.next_state,
        usage=_usage_of(runs),
    )


def _brief(request: QuoteRequest) -> str:
    return request.model_dump_json(indent=2)


def _policy_question(request: QuoteRequest) -> str:
    """One question, built from the request rather than asked of the model.

    The decoration limits are what a quote gets wrong in a way nobody notices
    until the artwork reaches the press."""
    method = (request.decoration_method or "screen print").replace("_", " ")
    colors = request.imprint_colors or 1
    return f"What are the limits and charges for a {colors} colour {method}, and can it be rushed?"


def _call_json(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "name": call.name,
        "arguments": call.arguments,
        "result": call.result,
        "error": call.error,
        "duration_ms": call.duration_ms,
        "replayed": call.replayed,
        "as_principal": call.as_principal,
    }
