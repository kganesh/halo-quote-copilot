"""M1: draft a quote with no catalogue, no supplier and no policy corpus.

This agent is meant to be wrong. It exists so the failure it produces has a name
and a shape before any of the machinery that fixes it is built — every entry in
the draft's `assumptions` list is a tool call M2 adds or a retrieval M3 adds.

It returns `escalated`, never `completed`. Design rule 02 says a figure without a
citation is not an answer, and this agent cannot produce a citation, so there is
no path here that legitimately completes.
"""

from __future__ import annotations

from halo.domain.request import UngroundedDraft
from halo.platform.bedrock import ModelClient
from halo.platform.budget import BudgetExceeded, BudgetTracker
from halo.platform.identity import Principal
from halo.platform.outcome import Outcome, OutcomeStatus

AGENT_NAME = "drafter"

SYSTEM_PROMPT = """\
You are drafting a preliminary quote for a HALO sales representative. HALO is a \
distributor of branded merchandise: apparel, headwear, drinkware, bags and \
promotional hard goods.

Read the representative's request and produce a complete draft quote: line items \
with quantities and unit prices, the decoration setup and per-unit run charges, a \
shipping cost, and a ship date.

You have no access to HALO's catalogue, pricing system, supplier capacity or \
policy documents in this draft.

List every figure you could not look up in `assumptions`, one entry per figure, \
each naming what you assumed and what would have to be checked to confirm it. Be \
specific: "assumed $11.50 unit cost for a mid-weight fleece hoodie at 500 units" \
is useful; "assumed pricing" is not.
"""


def draft_quote(
    request_text: str,
    *,
    principal: Principal,
    client: ModelClient,
    tracker: BudgetTracker,
) -> Outcome:
    """Turn a seller's sentence into an unsourced draft and an honest escalation."""
    try:
        result = client.parse(
            system=SYSTEM_PROMPT,
            user=request_text,
            output_format=UngroundedDraft,
        )
    except BudgetExceeded as exc:
        return Outcome(
            status=OutcomeStatus.ESCALATED,
            agent=AGENT_NAME,
            escalation_reason=f"budget exhausted before drafting: {exc}",
            next_state="await_budget_increase",
            usage=tracker.usage,
        )

    draft = result.parsed
    return Outcome(
        status=OutcomeStatus.ESCALATED,
        agent=AGENT_NAME,
        payload=draft.model_dump(mode="json"),
        # Deliberately empty. There is nothing to cite, and inventing a citation
        # to make the outcome look complete is the exact failure this build is
        # designed to catch.
        evidence=[],
        escalation_reason=(
            f"draft is ungrounded: {len(draft.assumptions)} figure(s) were assumed "
            f"rather than looked up, and no line carries a citation. "
            f"Principal {principal.user_id} must not send this to a customer."
        ),
        next_state="needs_grounding",
        usage=tracker.usage,
    )
