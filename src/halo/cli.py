"""The command line interface.

`halo quote` prints an M1 draft and then the reason it is not an answer. Showing
both at once is the point of that milestone: a quote that looks completely
credible, next to a list of everything in it that was invented.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal

import anthropic

from halo.agents.advisor import PolicyAnswer, answer_policy_question
from halo.agents.drafter import draft_quote
from halo.agents.sourcing import source_quote
from halo.agents.supervisor import draft as supervise
from halo.agents.supervisor import resume as resume_run
from halo.doctor import render as doctor_render
from halo.doctor import run as doctor_run
from halo.domain.quote import Quote
from halo.domain.request import QuoteRequest, UngroundedDraft
from halo.mcp_servers import SCOPED_TOOLS
from halo.platform import ledger, telemetry
from halo.platform.admission import AdmissionError, principal_from_claims
from halo.platform.bedrock import DEFAULT_MODEL, DEFAULT_REGION, BedrockClient, UnpricedModel
from halo.platform.budget import Budget, BudgetTracker
from halo.platform.checkpoint import ApprovalError, FileCheckpointStore, approve
from halo.platform.events import sink_from_env
from halo.platform.gateway import McpGateway, ToolSpec
from halo.platform.guardrails import BedrockGuardrail, Guardrail, LocalGuardrail
from halo.platform.identity import Principal
from halo.platform.outcome import Outcome, OutcomeStatus
from halo.rag.embed import TitanEmbedder
from halo.rag.retrieve import AtlasRetriever
from halo.rag.store import DEFAULT_DB, SqliteVectorStore

DEV_CLAIMS = {
    "sub": "usr-mwes01",
    "cognito:groups": "halo-seller",
    "custom:tenant_id": "tnt-mwest1",
    "custom:account_ids": "acct-mwes02,acct-mwes03",
    "token_use": "id",
}
"""Stand-in claims for a local run, shaped exactly like the authorizer's.

They go through `principal_from_claims` like anything else, so the development
path and the deployed path build a principal the same way. The seller and
accounts are real rows in the seed corpus, which is what makes `--claims` worth
having: swap in another seller and the denials are real denials.

This is not an authentication bypass. There is no authentication here to bypass:
the CLI runs as whoever is at the keyboard. What it must not become is a second
way of constructing a principal, which is why it is claims and not a Principal.
"""

SERVERS = {
    "accounts": [sys.executable, "-m", "halo.mcp_servers.accounts"],
    "pim_oms": [sys.executable, "-m", "halo.mcp_servers.pim_oms"],
    "supplier": [sys.executable, "-m", "halo.mcp_servers.supplier"],
    "shipping": [sys.executable, "-m", "halo.mcp_servers.shipping"],
}

# The filtered catalog: exactly the tools this agent's role was granted. A
# capacity scan reads more rows than a price lookup, so it gets a longer
# timeout.
TIMEOUTS = {
    "accounts.get_account": 10,
    "accounts.list_accounts": 10,
    "pim_oms.search_products": 10,
    "pim_oms.get_price": 10,
    "supplier.check_inventory": 20,
    "supplier.get_decoration_charges": 10,
    "supplier.earliest_ship_date": 20,
    "shipping.estimate_freight": 10,
}

# Whether a tool is scoped is read from `SCOPED_TOOLS`, not repeated here. A
# catalog that could disagree with it would be a hole visible in neither file.
CATALOG = {
    name: ToolSpec(name, timeout, scoped=name in SCOPED_TOOLS) for name, timeout in TIMEOUTS.items()
}

ADVISOR_BUDGET = Budget(
    wall_clock_seconds=120,
    max_tokens=60_000,
    max_tool_calls=0,
    max_usd=Decimal("0.50"),
)

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
    """Format money with two decimal places.

    `decimal_places=2` in the schema is a maximum, not a fixed scale. A model
    that returns 19.5 passes validation and then prints as `19.5`, in a column
    where every other figure ends in cents.
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

    This is separate so the CLI can print what to do about it. Each of these is
    something the operator fixes once. A stack trace does not help with any of
    them.
    """


def explain(error: Exception, *, region: str, model: str) -> str:
    """Turn an SDK or credential failure into an instruction."""
    if isinstance(error, UnpricedModel):
        return str(error)
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


def render_answer(outcome: Outcome, retrieved: list) -> str:
    lines = ["RETRIEVED"]
    for hit in retrieved:
        lines.append(f"  {hit.score:.4f} [{hit.found_by:<7}] {hit.chunk.id}")

    if outcome.payload:
        answer = PolicyAnswer.model_validate(
            {k: v for k, v in outcome.payload.items() if k != "retrieved"}
        )
        lines.append("")
        lines.append("ANSWER")
        for paragraph in answer.answer.split("\n"):
            lines.append(f"  {paragraph}")

        if answer.findings:
            lines.append("")
            lines.append(f"GROUNDED IN ({len(answer.findings)})")
            for finding in answer.findings:
                lines.append(f"  {finding.chunk_id}")
                lines.append(f'    "{finding.quote[:96]}"')

        if answer.unsupported:
            lines.append("")
            lines.append(f"NOT IN THE CORPUS ({len(answer.unsupported)})")
            for item in answer.unsupported:
                lines.append(f"  - {item}")

    lines.append("")
    lines.append(f"OUTCOME  {outcome.status.value}")
    if outcome.escalation_reason:
        lines.append(f"  {outcome.escalation_reason}")
    usage = outcome.usage
    lines.append(
        f"  spent: {usage.input_tokens:,} in + {usage.output_tokens:,} out tokens, "
        f"${usage.usd:.4f} (estimated)"
    )
    return "\n".join(lines)


def _retriever(region: str) -> AtlasRetriever:
    store = SqliteVectorStore(DEFAULT_DB)
    if not store.all_chunks():
        raise SetupError(
            f"the Atlas index at {DEFAULT_DB} is empty.\n  Build it with: python -m halo.rag.ingest"
        )
    return AtlasRetriever(store, TitanEmbedder(region=region))


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


def _principal(args) -> Principal:
    """The principal this run acts as.

    `--claims` takes the JSON an API Gateway JWT authorizer would hand a Lambda.
    Without it the development claims are used. Either way the same admission
    code runs, so a claim shape that fails in production fails here too.
    """
    raw = getattr(args, "claims", None)
    claims = json.loads(raw) if raw else DEV_CLAIMS
    return principal_from_claims(claims)


def _add_identity_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--claims",
        metavar="JSON",
        help="authorizer claims to act as; defaults to the development seller",
    )


def _add_guardrail_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--guardrail",
        choices=("auto", "local", "bedrock", "off"),
        default="auto",
        help="auto uses Bedrock when HALO_GUARDRAIL_ID is set, otherwise local",
    )


def _guardrail(args) -> Guardrail | None:
    """Which guardrail this run uses.

    `auto` is the default and prefers the managed one when the account has it:
    a local pattern set is a floor, and running on it while a real guardrail is
    configured would be choosing the weaker check silently.
    """
    choice = getattr(args, "guardrail", "auto")
    if choice == "off":
        return None
    if choice == "bedrock" or (choice == "auto" and os.environ.get("HALO_GUARDRAIL_ID")):
        return BedrockGuardrail(region=args.region)
    return LocalGuardrail()


async def _run(args, client_factory, principal: Principal):
    """M6: the supervisor, over the same gateway the single loop used."""
    tracker = BudgetTracker(SOURCING_BUDGET, owner="run")
    client = client_factory(region=args.region, model=args.model, tracker=tracker)
    request = QuoteRequest.model_validate_json(args.request_json)
    async with McpGateway(SERVERS, CATALOG, tracker=tracker, principal=principal) as gateway:
        outcome = await supervise(
            request,
            principal=principal,
            client=client,
            gateway=gateway,
            guardrail=_guardrail(args),
            retriever=_retriever(args.region) if args.policy else None,
            events=sink_from_env(),
        )
        return outcome, gateway.audit


async def _account(args, principal: Principal):
    """One scoped tool call, made as this principal."""
    async with McpGateway(SERVERS, CATALOG, principal=principal) as gateway:
        if args.account_id:
            return await gateway.call("accounts.get_account", {"account_id": args.account_id})
        return await gateway.call("accounts.list_accounts", {})


async def _source(args, client_factory, principal: Principal) -> tuple[Outcome, list]:
    tracker = BudgetTracker(SOURCING_BUDGET, owner="source")
    client = client_factory(region=args.region, model=args.model, tracker=tracker)
    request = QuoteRequest.model_validate_json(args.request_json)
    # The principal reaches the servers through the gateway, not through the
    # model. Every scoped call in this run is made as this user.
    async with McpGateway(SERVERS, CATALOG, tracker=tracker, principal=principal) as gateway:
        outcome = await source_quote(
            request,
            principal=principal,
            client=client,
            gateway=gateway,
            tracker=tracker,
            guardrail=_guardrail(args),
        )
        return outcome, gateway.audit


def main(argv: list[str] | None = None, *, client_factory=BedrockClient) -> int:
    """`client_factory` exists so the whole CLI path can run against a fake
    model, with no credentials and no spend."""
    parser = argparse.ArgumentParser(prog="halo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check the AWS setup a real call needs")
    doctor.add_argument("--region", default=DEFAULT_REGION)
    doctor.add_argument("--model", default=DEFAULT_MODEL)

    sub.add_parser("spend", help="what this project has spent on model calls")

    ask = sub.add_parser("ask", help="answer a policy question from the Atlas corpus")
    ask.add_argument("question")
    ask.add_argument("--region", default=DEFAULT_REGION)
    ask.add_argument("--model", default=DEFAULT_MODEL)
    ask.add_argument("--limit", type=int, default=6, help="excerpts to supply")
    ask.add_argument("--json", action="store_true")
    _add_guardrail_flag(ask)
    _add_identity_flags(ask)

    evaluate = sub.add_parser("eval", help="run the Atlas golden set")
    evaluate.add_argument("--region", default=DEFAULT_REGION)
    evaluate.add_argument("--model", default=DEFAULT_MODEL)
    evaluate.add_argument("--limit", type=int, default=6)

    source = sub.add_parser("source", help="source a quote from the MCP tool plane")
    source.add_argument("request_json", help="a QuoteRequest as JSON")
    source.add_argument("--region", default=DEFAULT_REGION)
    source.add_argument("--model", default=DEFAULT_MODEL)
    source.add_argument("--json", action="store_true")
    _add_guardrail_flag(source)
    _add_identity_flags(source)

    redteam = sub.add_parser("redteam", help="run the hostile-note suite against the sourcing loop")
    redteam.add_argument(
        "--live",
        action="store_true",
        help="run against Bedrock instead of the control model (costs money)",
    )
    redteam.add_argument("--region", default=DEFAULT_REGION)
    redteam.add_argument("--model", default=DEFAULT_MODEL)

    run_ = sub.add_parser("run", help="draft a quote through the supervisor and specialists")
    run_.add_argument("request_json", help="a QuoteRequest as JSON")
    run_.add_argument("--region", default=DEFAULT_REGION)
    run_.add_argument("--model", default=DEFAULT_MODEL)
    run_.add_argument("--json", action="store_true")
    run_.add_argument(
        "--trace",
        action="store_true",
        help="print the trace: every state, model, tool, decision and approval",
    )
    run_.add_argument(
        "--policy",
        action="store_true",
        help="also run the policy specialist over Atlas (needs `make ingest`)",
    )
    _add_guardrail_flag(run_)
    _add_identity_flags(run_)

    pending = sub.add_parser("pending", help="quotes waiting for a margin approval")
    _add_identity_flags(pending)

    sub.add_parser("gate", help="the offline evaluation gates CI runs")

    teardown = sub.add_parser("teardown", help="check the stack tears down to nothing")
    teardown.add_argument(
        "--live",
        action="store_true",
        help="also ask Cost Explorer what the account was charged since the destroy",
    )
    teardown.add_argument("--days", type=int, default=2, help="days of billing to read")

    approve_ = sub.add_parser("approve", help="release a checkpoint and finish the quote")
    approve_.add_argument("checkpoint_id")
    approve_.add_argument("--json", action="store_true")
    _add_identity_flags(approve_)

    account = sub.add_parser("account", help="read a customer account as this principal")
    account.add_argument("account_id", nargs="?", help="omit to list the accounts in scope")
    _add_identity_flags(account)

    quote = sub.add_parser("quote", help="draft a quote from a seller's sentence")
    quote.add_argument("request", help="what the customer asked for, in plain English")
    quote.add_argument("--region", default=DEFAULT_REGION)
    quote.add_argument("--model", default=DEFAULT_MODEL)
    quote.add_argument("--json", action="store_true", help="emit the raw Outcome as JSON")
    _add_identity_flags(quote)

    args = parser.parse_args(argv)

    try:
        principal = _principal(args) if hasattr(args, "claims") else None
    except (AdmissionError, ValueError) as error:
        print(f"not admitted: {error}", file=sys.stderr)
        return 1

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
        grouped = ledger.by_command(entries)
        width = max(len(c) for c in grouped)

        # The cache columns appear only once something has actually been cached.
        # Two columns of zeroes would be noise on every run until M6.
        cached = overall.cache_read_tokens or overall.cache_write_tokens

        def row(name: str, total: ledger.Total) -> str:
            line = (
                f"{name.ljust(width)}  {total.runs:>5} {total.input_tokens:>10,} "
                f"{total.output_tokens:>9,}"
            )
            if cached:
                line += f" {total.cache_read_tokens:>10,} {total.cache_write_tokens:>10,}"
            return line + f" {total.tool_calls:>6} {total.usd:>9.4f}"

        header = f"{'command'.ljust(width)}  {'runs':>5} {'in':>10} {'out':>9}"
        if cached:
            header += f" {'cache rd':>10} {'cache wr':>10}"
        print(f"{header} {'tools':>6} {'usd':>9}")

        for command, total in grouped.items():
            print(row(command, total))
        print(row("TOTAL", overall))

        if cached:
            billed = overall.input_tokens + overall.cache_read_tokens + overall.cache_write_tokens
            print(f"\n{overall.cache_read_tokens / billed:.0%} of input tokens came from cache.")

        print("\nEstimated from the Bedrock rate card for us-east-1, not an invoice.")
        return 0

    if args.command == "ask":
        tracker = BudgetTracker(ADVISOR_BUDGET, owner="ask")
        try:
            client = client_factory(region=args.region, model=args.model, tracker=tracker)
            outcome, retrieved = answer_policy_question(
                args.question,
                principal=principal,
                client=client,
                retriever=_retriever(args.region),
                tracker=tracker,
                limit=args.limit,
                guardrail=_guardrail(args),
            )
        except SetupError as error:
            print(str(error), file=sys.stderr)
            return 1
        except (anthropic.APIError, RuntimeError) as error:
            print(explain(error, region=args.region, model=args.model), file=sys.stderr)
            return 1

        ledger.record("ask", args.model, outcome.usage)
        if args.json:
            print(json.dumps(outcome.model_dump(mode="json"), indent=2))
        else:
            print(render_answer(outcome, retrieved))
        return 0 if outcome.status is OutcomeStatus.COMPLETED else 2

    if args.command == "eval":
        from halo.evals.atlas_golden import GOLDEN
        from halo.rag.evaluate import run_golden, summarise

        tracker = BudgetTracker(
            owner="eval",
            budget=Budget(
                wall_clock_seconds=1800,
                max_tokens=2_000_000,
                max_tool_calls=0,
                max_usd=Decimal("2.00"),
            ),
        )
        try:
            client = client_factory(region=args.region, model=args.model, tracker=tracker)
            results = run_golden(
                GOLDEN,
                principal=principal,
                client=client,
                retriever=_retriever(args.region),
                tracker=tracker,
                limit=args.limit,
                guardrail=_guardrail(args),
            )
        except SetupError as error:
            print(str(error), file=sys.stderr)
            return 1
        except (anthropic.APIError, RuntimeError) as error:
            print(explain(error, region=args.region, model=args.model), file=sys.stderr)
            return 1

        for result in results:
            mark = "PASS" if result.passed else "FAIL"
            flags = (
                f"r={'Y' if result.retrieved else 'n'} "
                f"c={'Y' if result.cited else 'n'} "
                f"q={'Y' if result.fact_in_quote else 'n'}"
            )
            print(f"  [{mark}] {flags}  {result.question[:62]}")
            if not result.passed and result.detail:
                print(f"         {result.detail[:100]}")

        stats = summarise(results)
        ledger.record("eval", args.model, tracker.usage)
        print(
            f"\nretrieval {stats['retrieval']}/{stats['total']}"
            f"   cited {stats['cited']}/{stats['total']}"
            f"   grounded {stats['grounded']}/{stats['total']}"
            f"   ${tracker.usage.usd:.4f}"
        )
        return 0 if stats["grounded"] == stats["total"] else 2

    if args.command == "account":
        # Straight through the gateway, with no model in the path. The point of
        # this command is that the refusal comes from the tool: there is nothing
        # here that could have decided to be careful instead.
        call = asyncio.run(_account(args, principal))
        print(f"as {principal.user_id} ({principal.role}, {principal.tenant_id})")
        if call.ok:
            print(json.dumps(call.result, indent=2))
            return 0
        print(f"{call.name} [{call.id}] {call.error}", file=sys.stderr)
        return 2

    if args.command == "run":
        # Collected in memory rather than exported, because the deliverable is a
        # trace someone reads on a terminal. A deployment passes an OTLP exporter
        # to the same `configure`.
        spans = None
        if args.trace:
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
                InMemorySpanExporter,
            )

            spans = InMemorySpanExporter()
            telemetry.configure(spans)

        try:
            outcome, audit = asyncio.run(_run(args, client_factory, principal))
        except (anthropic.APIError, RuntimeError) as error:
            print(explain(error, region=args.region, model=args.model), file=sys.stderr)
            return 1
        ledger.record("run", args.model, outcome.usage)
        if args.json:
            print(json.dumps(outcome.model_dump(mode="json"), indent=2))
        else:
            print(render_sourced(outcome, audit))
        if spans is not None:
            print("\nTRACE")
            print(telemetry.render_trace(spans.get_finished_spans()))
        return 0 if outcome.status is OutcomeStatus.COMPLETED else 2

    if args.command == "teardown":
        from halo import infra

        findings = infra.check()
        print(infra.report(findings))

        if args.live:
            # Cost Explorer is the only authority here: what bills after a
            # teardown is by definition what Terraform did not create.
            try:
                charged = infra.verify_teardown(days=args.days)
            except Exception as error:  # noqa: BLE001 - any failure is the answer
                print(f"could not read Cost Explorer: {error}", file=sys.stderr)
                return 1
            if not charged:
                print(f"\nNothing charged in the last {args.days} days.")
            else:
                print(f"\nStill charging over the last {args.days} days:")
                for service, amount in charged:
                    print(f"  {service:<44} ${amount}")
                return 2

        return 0 if not findings else 2

    if args.command == "gate":
        from halo.evals.gates import passed, report, run_all

        gates = asyncio.run(run_all())
        print(report(gates))
        # Non-zero fails the build. That is the whole point of the command: a
        # grounding regression should stop a merge, not surface in a demo.
        return 0 if passed(gates) else 2

    if args.command == "pending":
        checkpoints = FileCheckpointStore().open_checkpoints()
        if not checkpoints:
            print("Nothing waiting for approval.")
            return 0
        for checkpoint in checkpoints:
            owner = checkpoint.owner()
            print(f"{checkpoint.id}  {checkpoint.created_at}  {owner.user_id}  {checkpoint.reason}")
        return 0

    if args.command == "approve":
        store = FileCheckpointStore()
        try:
            released = approve(store.load(args.checkpoint_id), principal)
        except ApprovalError as error:
            print(f"not approved: {error}", file=sys.stderr)
            return 1
        store.save(released)

        # No client and no gateway: the quote is assembled from the evidence the
        # paused run already gathered. Re-running it would produce a different
        # quote from the one that was approved.
        outcome = resume_run(released, events=sink_from_env())
        if args.json:
            print(json.dumps(outcome.model_dump(mode="json"), indent=2))
        else:
            print(f"approved {released.id} by {principal.user_id}")
            print(render_sourced(outcome, []))
        return 0 if outcome.status is OutcomeStatus.COMPLETED else 2

    if args.command == "redteam":
        from halo.evals.redteam import report, run_offline

        if args.live:
            print(
                "Live red-teaming is not wired up yet. The offline suite runs the "
                "same notes through the same loop against a model that obeys every "
                "one of them, which is the stronger test of the harness.",
                file=sys.stderr,
            )
            return 1

        results = asyncio.run(run_offline())
        print(report(results))
        # Non-zero when anything got through, so CI fails on a regression rather
        # than printing a table nobody reads.
        return 0 if all(result.passed for result in results) else 2

    if args.command == "source":
        try:
            outcome, audit = asyncio.run(_source(args, client_factory, principal))
        except (anthropic.APIError, RuntimeError) as error:
            print(explain(error, region=args.region, model=args.model), file=sys.stderr)
            return 1
        ledger.record("source", args.model, outcome.usage)
        if args.json:
            print(json.dumps(outcome.model_dump(mode="json"), indent=2))
        else:
            print(render_sourced(outcome, audit))
        return 0 if outcome.status is OutcomeStatus.COMPLETED else 2

    tracker = BudgetTracker(DEFAULT_BUDGET, owner="quote")
    try:
        client = client_factory(region=args.region, model=args.model, tracker=tracker)
        outcome = draft_quote(args.request, principal=principal, client=client, tracker=tracker)
    except (anthropic.APIError, RuntimeError) as error:
        print(explain(error, region=args.region, model=args.model), file=sys.stderr)
        return 1

    ledger.record("quote", args.model, outcome.usage)
    if args.json:
        print(json.dumps(outcome.model_dump(mode="json"), indent=2))
    else:
        print(render(outcome))

    # Return non-zero. An escalation is not a success, and M1 always escalates.
    # Setting the exit code now means a later CI gate will behave correctly.
    return 0 if outcome.status.value == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
