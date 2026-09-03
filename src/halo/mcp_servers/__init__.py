"""MCP servers standing in for HALO's enterprise systems.

Named `mcp_servers` rather than `mcp` on purpose: a top-level package called
`mcp` would sit next to the MCP SDK of the same name and turn every import into
a guess about which one you got.

Each server exposes plain functions that are also registered as tools. Tests call
the functions directly; the protocol wiring gets one integration test of its own,
rather than a subprocess spawn per assertion.
"""
