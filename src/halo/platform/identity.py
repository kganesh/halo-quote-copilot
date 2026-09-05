"""Design rule 03: identity travels with the request.

A Cognito JWT is exchanged once, at admission, for a `Principal`. That principal
is passed into the agent session and into every MCP call. The tool servers
enforce it. The agent never filters results itself. At M5, a cross-tenant request
returns a denial from the tool, and the agent has to report that denial.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class Role(StrEnum):
    SELLER = "seller"
    SALES_MANAGER = "sales_manager"
    OPERATIONS = "operations"


class Principal(BaseModel):
    """Who this run is acting for. Cannot be changed after it is created."""

    model_config = {"frozen": True}

    user_id: str
    tenant_id: str
    role: Role
    account_ids: tuple[str, ...] = Field(min_length=1)

    def may_read_account(self, account_id: str) -> bool:
        """A sales manager sees the whole tenant. A seller sees only their own
        accounts."""
        if self.role is Role.SALES_MANAGER:
            return True
        return account_id in self.account_ids


FORBIDDEN = "403 forbidden"
"""The prefix every scope refusal carries.

A tool refuses by returning an error, which reaches the agent as a string. The
agent has to be able to tell a refusal from an outage without parsing prose:
one means report it and stop, the other means the system is down. This prefix is
that distinction, and it lives here rather than in a server so that both sides
of the boundary read it from the same place."""


def is_denial(error: str | None) -> bool:
    """Whether a failed tool call was refused rather than broken."""
    return bool(error) and error.startswith(FORBIDDEN)


class ScopeDenied(Exception):
    """Raised by a tool when the principal is outside its scope.

    This carries no data about the record it refused to show. An error message
    that describes what it withheld leaks some of that information.
    """

    def __init__(self, principal: Principal, account_id: str) -> None:
        super().__init__(f"principal {principal.user_id} may not read account {account_id}")
        self.user_id = principal.user_id
        self.account_id = account_id
