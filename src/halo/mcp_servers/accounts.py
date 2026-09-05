"""The customer directory, and the only server that answers per-account.

This server exists to make design rule 03 testable. Every other tool answers a
question about goods — a price, a stock level, a transit time — and the answer
is the same whoever asks. These answers are not: an account belongs to a seller,
and a seller who is not on it may not read it.

The check happens here, at the tool, not in the agent. An agent that filters its
own results has already seen the data, and "the model did not mention it" is not
access control. What the agent gets back is a denial, and the only thing it can
do with a denial is report it.

Two failures return the same thing on purpose: an account in another seller's
book, and an account that does not exist. Distinguishing them turns this tool
into an oracle for enumerating the customer list, one id at a time.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from halo.mcp_servers.store import accounts
from halo.platform.identity import FORBIDDEN, Principal

server = MCPServer(name="halo-accounts", description="HALO customer accounts, scoped to the caller")

DENIAL = f"{FORBIDDEN}: this account is not in your scope"
"""One message for every refusal.

It names no account, no owner and no tenant. A denial that explained itself
would answer the question it just refused to answer.
"""


def _caller(principal: dict[str, Any] | Principal) -> Principal:
    """Rebuild the principal the gateway attached to this call.

    It arrives as a dict because MCP arguments are JSON. Validating it back into
    the frozen model here means a malformed identity fails at the boundary, in
    the same way a malformed price would.
    """
    return principal if isinstance(principal, Principal) else Principal.model_validate(principal)


def _denied(account_id: str) -> dict:
    return {"error": DENIAL, "status": 403, "account_id": account_id}


@server.tool()
def get_account(account_id: str, principal: dict) -> dict:
    """The customer record for an account the caller is allowed to read.

    Args:
        account_id: the account, e.g. "acct-mwes02".
        principal: attached by the gateway. Not a field a caller may set.
    """
    caller = _caller(principal)
    record = next((a for a in accounts() if a["id"] == account_id), None)

    # Tenant first, then scope. A sales manager reads their whole tenant, which
    # is a wide grant, and it must not become a grant over someone else's.
    if record is None or record["tenant_id"] != caller.tenant_id:
        return _denied(account_id)
    if not caller.may_read_account(account_id):
        return _denied(account_id)

    return {
        "account_id": record["id"],
        "name": record["name"],
        "industry": record["industry"],
        "ship_to_city": record["ship_to_city"],
        "ship_to_state": record["ship_to_state"],
        "tenant_id": record["tenant_id"],
    }


@server.tool()
def list_accounts(principal: dict) -> list[dict]:
    """Every account the caller may read.

    Takes no account argument, so there is nothing here to guess at. A seller
    sees their own book; a sales manager sees the tenant's.
    """
    caller = _caller(principal)
    return [
        {"account_id": a["id"], "name": a["name"], "ship_to_state": a["ship_to_state"]}
        for a in accounts()
        if a["tenant_id"] == caller.tenant_id and caller.may_read_account(a["id"])
    ]


if __name__ == "__main__":
    server.run(transport="stdio")
