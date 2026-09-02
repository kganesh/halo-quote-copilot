"""Design rule 03: identity is a token that travels.

A Cognito JWT is exchanged once, at admission, for a `Principal`. That principal
rides into the agent session and onto every MCP call, where the tool servers
enforce it themselves. The agent is never the thing doing the filtering — at M5 a
cross-tenant request comes back as a tool-level denial the agent has to report.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class Role(StrEnum):
    SELLER = "seller"
    SALES_MANAGER = "sales_manager"
    OPERATIONS = "operations"


class Principal(BaseModel):
    """Who this run is acting for. Immutable once minted."""

    model_config = {"frozen": True}

    user_id: str
    tenant_id: str
    role: Role
    account_ids: tuple[str, ...] = Field(min_length=1)

    def may_read_account(self, account_id: str) -> bool:
        """Sales managers see their whole tenant; sellers see their own book."""
        if self.role is Role.SALES_MANAGER:
            return True
        return account_id in self.account_ids


class ScopeDenied(Exception):
    """Raised by a tool when the principal is outside its scope.

    Carries no data about the out-of-scope record on purpose: an error message
    that names what it refused to show is its own small leak.
    """

    def __init__(self, principal: Principal, account_id: str) -> None:
        super().__init__(f"principal {principal.user_id} may not read account {account_id}")
        self.user_id = principal.user_id
        self.account_id = account_id
