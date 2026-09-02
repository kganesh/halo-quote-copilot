"""Who is asking, and what they are allowed to ask about.

Account scope lives here rather than on the agent because M5 enforces it at the
tool boundary — an agent is never trusted to filter results it can already see.
"""

from pydantic import BaseModel, Field


class Tenant(BaseModel):
    """A HALO business unit. Every record in the system belongs to exactly one."""

    id: str = Field(pattern=r"^tnt-[a-z0-9]{6}$")
    name: str
    region: str


class Account(BaseModel):
    """An end customer a seller quotes for."""

    id: str = Field(pattern=r"^acct-[a-z0-9]{6}$")
    tenant_id: str
    name: str
    industry: str
    ship_to_city: str
    ship_to_state: str


class Seller(BaseModel):
    """A HALO sales rep. `account_ids` is the whole of their read scope."""

    id: str = Field(pattern=r"^usr-[a-z0-9]{6}$")
    tenant_id: str
    name: str
    email: str
    role: str
    account_ids: list[str] = Field(min_length=1)
