"""MCP servers that stand in for HALO's enterprise systems.

The package is called `mcp_servers` and not `mcp` on purpose. A top-level package
named `mcp` would clash with the MCP SDK of the same name, and every import would
become ambiguous.

Each server defines plain functions and also registers them as tools. Tests call
the functions directly. The protocol wiring has one integration test of its own,
instead of starting a subprocess for every assertion.
"""

SCOPED_TOOLS = frozenset({"accounts.get_account", "accounts.list_accounts"})
"""Tools that answer per-account and so must be called as somebody.

Listed once, here, because two places build a tool catalog — the CLI and the
red-team runner — and a tool that is scoped in one and not the other would be a
hole nobody could see by reading either file.
"""
