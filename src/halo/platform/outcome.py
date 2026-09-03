"""Design rule 01: an agent returns an Outcome, never a string.

Making this the only allowed return type turns a conversational model into a
component that can be composed and tested. Every field an orchestrator needs in
order to decide the next step is a named field here. None of it is buried in
prose.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from halo.domain.quote import Citation
from halo.platform.budget import Usage


class OutcomeStatus(StrEnum):
    COMPLETED = "completed"
    ESCALATED = "escalated"
    REFUSED = "refused"


class Outcome(BaseModel):
    """The result of one agent run.

    `escalated` means the work is correct but a human has to decide. Examples:
    the margin is too low, the date cannot be met, the budget ran out.

    `refused` means the request should not be answered at all. Examples: it is
    out of scope, or a guardrail blocked it.

    These are kept separate because the approval flow needs to tell them apart.
    """

    status: OutcomeStatus
    agent: str
    payload: dict[str, Any] | None = None
    evidence: list[Citation] = Field(default_factory=list)
    next_state: str | None = None
    escalation_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)

    @model_validator(mode="after")
    def _reason_accompanies_a_stop(self) -> "Outcome":
        if self.status is not OutcomeStatus.COMPLETED and not self.escalation_reason:
            raise ValueError(f"{self.status} outcome must carry an escalation_reason")
        if self.status is OutcomeStatus.COMPLETED and self.payload is None:
            raise ValueError("completed outcome must carry a payload")
        return self
