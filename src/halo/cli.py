"""`halo quote "..."` — the M1 demo.

Prints the draft, then the reason it is not an answer. The point of the milestone
is that both halves are visible at once: a quote that looks entirely credible,
and a list of everything in it that was made up.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal

import anthropic

from halo.agents.drafter import draft_quote
from halo.agents.sourcing import source_quote
from halo.doctor import render as doctor_render
from halo.doctor import run as doctor_run
from halo.domain.quote import Quote
from halo.domain.request import QuoteRequest, UngroundedDraft
from halo.platform import ledger
from halo.platform.bedrock import DEFAULT_MODEL, DEFAULT_REGION, BedrockClient
from halo.platform.budget import Budget, BudgetTracker
from halo.platform.gateway import McpGateway, ToolSpec
from halo.platform.identity import Principal, Role
from halo.platform.outcome import Outcome, OutcomeStatus

# Stands in until M5 mints a real principal from a Cognito token.
DEMO_PRINCIPAL = Principal(
    user_id="usr-mwest01",
    tenant_id="tnt-mwest1",
    role=Role.SELLER,
    account_ids=("acct-mwest02", "acct-mwest03"),
)

SERVERS = {
    "pim_oms": [sys.executable, "-m", "halo.mcp_servers.pim_oms"],
    "supplier": [sys.executable, "-m", "halo.mcp_servers.supplier"],
    "shipping": [sys.executable, "-m", "halo.mcp_servers.shipping"],
}

# The filtered catalog: exactly the tools this agent's role was granted. A
# capacity scan reads more rows than a price lookup, hence the longer patience.
CATALOG = {
    "pim_oms.search_products": ToolSpec("pim_oms.search_products", 10),
    "pim_oms.get_price": ToolSpec("pim_oms.get_price", 10),
    "supplier.check_inventory": ToolSpec("supplier.check_inventory", 20),
    "supplier.get_decoration_charges": ToolSpec("supplier.get_decoration_charges", 10),
    "supplier.earliest_ship_date": ToolSpec("supplier.earliest_ship_date", 20),
    "shipping.estimate_freight": ToolSpec("shipping.estimate_freight", 10),
}

SOURCING_BUDGET = Budget(
    wall_clock_seconds=300,
    max_tokens=200_000,
    max_tool_calls=20,
    max_usd=Decimal("1.00"),
)

DEFAULT_BUDGET = Budget(
    wall_clock_seconds=120,
    max_tokens=40_000,
    max_tool_calls=0,
    max_usd=Decimal("0.25"),
)


def money(amount: Decimal) -> str:
    """Money always shows cents.

    `decimal_places=2` on the schema is a maximum, not a fixed scale, so a model
    that returns 19.5 validates fine and then prints as `19.5` in a column of
    figures that all end in cents.
    """
    return f"{amount:,.2f}"


def render(outcome: Outcome) -> str:
    lines: list[str] = []
    if outcome.payload:
        draft = UngroundedDraft.model_validate(outcome.payload)
        req = draft.request
        lines.append("READ AS")
        lines.append(f"  {req.quantity or '?'} x {req.product_description}")
        if req.decoration_method:
            colors = f", {req.imprint_colors} colour" if req.imprint_colors else ""
            lines.append(f"  {req.decoration_method.value.replace('_', ' ')}{colors}")
        if req.ship_to_city:
            lines.append(f"  ship to {req.ship_to_city}, {req.ship_to_state or ''}".rstrip(", "))
        if req.needed_by:
            lines.append(f"  needed by {req.needed_by:%d %b %Y}")
        if req.budget_usd:
            lines.append(f"  budget ${req.budget_usd:,.2f}")

        lines.append("")
        lines.append("DRAFT")
        for line in draft.lines:
            lines.append(
                f"  {line.sku:<16} {line.description[:34]:<34} "
                f"{line.quantity:>6} x {money(line.unit_price):>9} = {money(line.extended):>12}"
            )
        for label, amount in (
            ("decoration setup", draft.decoration_setup_fee),
            ("decoration run charge / unit", draft.decoration_run_charge_per_unit),
            ("shipping", draft.shipping_cost),
            ("TOTAL", draft.total),
        ):
            lines.append(f"  {'':<16} {label:<34} {money(amount):>32}")
        lines.append(f"  ships {draft.promised_ship_date:%d %b %Y}")

        lines.append("")
        lines.append(f"MADE UP ({len(draft.assumptions)})")
        for assumption in draft.assumptions:
            lines.append(f"  - {assumption}")

    lines.append("")
    lines.append(f"OUTCOME  {outcome.status.value}")
    lines.append(f"  {outcome.escalation_reason}")
    lines.append(f"  next state: {outcome.next_state}")
    usage = outcome.usage
    lines.append(
        f"  spent: {usage.input_tokens:,} in + {usage.output_tokens:,} out tokens, "
        f"${usage.usd:.4f} (estimated)"
    )
    return "\n".join(lines)


class SetupError(Exception):
    """A problem with the environment, not with the request.

    Separated so the CLI can say what to do about it. Every one of these is
    something the operator fixes once, and a stack trace helps with none of them.
    """


def explain(error: Exception, *, region: str, model: str) -> str:
    """Turn an SDK or credential failure into an instruction."""
    if isinstance(error, anthropic.NotFoundError):
        return (
            f"{model} is not available to this account in {region}.\n"
            f"  Enable the model in the Bedrock console under Model access, or pass "
            f"--region for one where it is enabled."
        )
    if isinstance(error, anthropic.PermissionDeniedError):
        return (
            f"These credentials may not invoke {model} in {region}.\n"
            f"  The caller needs bedrock:InvokeModel on that model's ARN."
        )
    if isinstance(error, anthropic.RateLimitError):
        return "Bedrock throttled the request. Retry, or ask for a quota increase."
    if isinstance(error, anthropic.APIConnectionError):
        return f"Could not reach Bedrock in {region}. Check the network and the region name."
    if isinstance(error, RuntimeError) and "credentials" in str(error).lower():
        return (
            "No AWS credentials found.\n"
            "  Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, run `aws configure`,\n"
            "  or export AWS_PROFILE for a profile in ~/.aws/credentials."
        )
    return f"{type(error).__name__}: {error}"


def render_sourced(outcome: Outcome, audit: list) -> str:
    lines = ["TOOL CALLS"]
    for call in audit:
        mark = "replay" if call.replayed else ("ok" if call.ok else "FAIL")
        detail = call.error if not call.ok else f"{call.duration_ms:.0f}ms"
        lines.append(f"  {call.id}  {mark:<6} {call.name:<34} {detail}")

    if outcome.payload and outcome.status is OutcomeStatus.COMPLETED:
        quote = Quote.model_validate(outcome.payload)
        lines.append("")
        lines.append("QUOTE")
        for line in quote.lines:
            lines.append(
                f"  {line.sku:<16} {line.description[:30]:<30} {line.quantity:>6} x "
                f"{money(line.unit_price):>9} = {money(line.extended):>12}"
            )
        for label, amount in (
            (f"decoration setup ({quote.decoration.colors} colour)", quote.decoration.setup_fee),
            ("decoration run / unit", quote.decoration.run_charge_per_unit),
            ("shipping", quote.shipping_cost),
            ("TOTAL", quote.total),
        ):
            lines.append(f"  {'':<16} {label:<30} {money(amount):>32}")
        lines.append(f"  ships {quote.promised_ship_date:%d %b %Y}")
        if questions := outcome.payload.get("open_questions"):
            lines.append("")
            lines.append(f"OPEN QUESTIONS ({len(questions)}) — for order entry, not the quote")
            for question in questions:
                lines.append(f"  - {question}")

        lines.append("")
        lines.append(f"EVIDENCE ({len(quote.all_citations)} citations)")
        for citation in quote.all_citations:
            lines.append(f"  {citation.ref}  {citation.supporting_text[:88]}")

    lines.append("")
    lines.append(f"OUTCOME  {outcome.status.value}")
    if outcome.escalation_reason:
        lines.append(f"  {outcome.escalation_reason}")
    lines.append(f"  next state: {outcome.next_state}")
    usage = outcome.usage
    lines.append(
        f"  spent: {usage.input_tokens:,} in + {usage.output_tokens:,} out tokens, "
        f"{usage.tool_calls} tool calls, ${usage.usd:.4f} (estimated)"
    )
    return "\n".join(lines)


async def _source(args, client_factory) -> tuple[Outcome, list]:
    tracker = BudgetTracker(SOURCING_BUDGET)
    client = client_factory(region=args.region, model=args.model, tracker=tracker)
    request = QuoteRequest.model_validate_json(args.request_json)
    async with McpGateway(SERVERS, CATALOG, tracker=tracker) as gateway:
        outcome = await source_quote(
            request,
            principal=DEMO_PRINCIPAL,
            client=client,
            gateway=gateway,
            tracker=tracker,
        )
        return outcome, gateway.audit


def main(argv: list[str] | None = None, *, client_factory=BedrockClient) -> int:
    """`client_factory` exists so the whole CLI path can be exercised against a
    fake model, without credentials and without spending anything."""
    parser = argparse.ArgumentParser(prog="halo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check the AWS setup a real call needs")
    doctor.add_argument("--region", default=DEFAULT_REGION)
    doctor.add_argument("--model", default=DEFAULT_MODEL)

    sub.add_parser("spend", help="what this project has spent on model calls")

    source = sub.add_parser("source", help="source a quote from the MCP tool plane")
    source.add_argument("request_json", help="a QuoteRequest as JSON")
    source.add_argument("--region", default=DEFAULT_REGION)
    source.add_argument("--model", default=DEFAULT_MODEL)
    source.add_argument("--json", action="store_true")

    quote = sub.add_parser("quote", help="draft a quote from a seller's sentence")
    quote.add_argument("request", help="what the customer asked for, in plain English")
    quote.add_argument("--region", default=DEFAULT_REGION)
    quote.add_argument("--model", default=DEFAULT_MODEL)
    quote.add_argument("--json", action="store_true", help="emit the raw Outcome as JSON")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        checks, ok = doctor_run(args.region, args.model)
        print(doctor_render(checks, ok, region=args.region, model=args.model))
        return 0 if ok else 1

    if args.command == "spend":
        entries = ledger.read()
        if not entries:
            print(f"No runs recorded yet ({ledger.LEDGER_PATH}).")
            return 0
        overall = ledger.totals(entries)
        width = max(len(c) for c in ledger.by_command(entries))
        print(
            f"{'command'.ljust(width)}  {'runs':>5} {'in':>10} {'out':>9} {'tools':>6} {'usd':>9}"
        )
        for command, total in ledger.by_command(entries).items():
            print(
                f"{command.ljust(width)}  {total.runs:>5} {total.input_tokens:>10,} "
                f"{total.output_tokens:>9,} {total.tool_calls:>6} {total.usd:>9.4f}"
            )
        print(
            f"{'TOTAL'.ljust(width)}  {overall.runs:>5} {overall.input_tokens:>10,} "
            f"{overall.output_tokens:>9,} {overall.tool_calls:>6} {overall.usd:>9.4f}"
        )
        print("\nEstimated from first-party rates; Bedrock prices separately.")
        return 0

    if args.command == "source":
        try:
            outcome, audit = asyncio.run(_source(args, client_factory))
        except (anthropic.APIError, RuntimeError) as error:
            print(explain(error, region=args.region, model=args.model), file=sys.stderr)
            return 1
        ledger.record("source", args.model, outcome.usage)
        if args.json:
            print(json.dumps(outcome.model_dump(mode="json"), indent=2))
        else:
            print(render_sourced(outcome, audit))
        return 0 if outcome.status is OutcomeStatus.COMPLETED else 2

    tracker = BudgetTracker(DEFAULT_BUDGET)
    try:
        client = client_factory(region=args.region, model=args.model, tracker=tracker)
        outcome = draft_quote(
            args.request, principal=DEMO_PRINCIPAL, client=client, tracker=tracker
        )
    except (anthropic.APIError, RuntimeError) as error:
        print(explain(error, region=args.region, model=args.model), file=sys.stderr)
        return 1

    ledger.record("quote", args.model, outcome.usage)
    if args.json:
        print(json.dumps(outcome.model_dump(mode="json"), indent=2))
    else:
        print(render(outcome))

    # Non-zero: an escalation is not a success, and M1 always escalates. Wiring
    # this into the exit code now keeps a later CI gate honest.
    return 0 if outcome.status.value == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
