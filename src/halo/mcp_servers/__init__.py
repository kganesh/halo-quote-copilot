"""MCP servers that stand in for HALO's enterprise systems.

The package is called `mcp_servers` and not `mcp` on purpose. A top-level package
named `mcp` would clash with the MCP SDK of the same name, and every import would
become ambiguous.

Each server defines plain functions and also registers them as tools. Tests call
the functions directly. The protocol wiring has one integration test of its own,
instead of starting a subprocess for every assertion.
"""
