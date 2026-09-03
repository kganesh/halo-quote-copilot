"""Every tool call an agent makes goes through here.

The reference architecture puts a filtered catalog, typed schemas, timeouts,
idempotency and audit between the agents and the enterprise systems. This is that
layer, small enough to read in one sitting:

- **Allow-list.** An agent can call the tools its role was granted and nothing
  else. A tool that is not on the list is refused before the transport is touched.
- **Timeout.** Per tool, because a catalogue search and a capacity scan do not
  deserve the same patience.
- **Idempotency.** The same call with the same arguments inside one run returns
  the recorded result. A model that asks twice gets one answer and one audit row.
- **Audit.** Every call gets a `tool_call_id`, and that id is what a quote cites.
  Provenance is a by-product of governance rather than a separate feature.

Two implementations share all of that and differ only in how they reach the tool:
`McpGateway` speaks MCP over stdio to real server processes, `InProcessGateway`
calls the same Python functions directly. Tests use the second so the policy is
tested without a subprocess per assertion, and one integration test covers the
first.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from halo.platform.budget import BudgetTracker


@dataclass(frozen=True)
class ToolSpec:
    """One entry in the filtered catalog."""

    name: str
    """Qualified as `server.tool`, so an audit row says which system answered."""
    timeout_seconds: float = 10.0

    @property
    def server(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def tool(self) -> str:
        return self.name.split(".", 1)[1]


@dataclass
class ToolCall:
    """The audit record, and the thing a quote cites.

    `result` is kept whole rather than summarized: at M7 this envelope is what
    lands in the evidence store, and a summary written now would be the wrong
    summary then.
    """

    id: str
    name: str
    arguments: dict[str, Any]
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    replayed: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


class ToolUnavailable(Exception):
    """The tool exists and was allowed, but the call could not be completed.

    Distinct from a refusal: a refused call is a policy answer, an unavailable
    one is an outage, and an agent has to escalate rather than proceed on either.
    """


class ToolGateway(Protocol):
    async def call(self, name: str, arguments: dict[str, Any]) -> ToolCall: ...
    @property
    def audit(self) -> list[ToolCall]: ...


@dataclass
class _GatewayPolicy:
    """The half that has nothing to do with transport."""

    allowed: dict[str, ToolSpec]
    tracker: BudgetTracker | None = None
    _audit: list[ToolCall] = field(default_factory=list)
    _seen: dict[str, ToolCall] = field(default_factory=dict)

    @property
    def audit(self) -> list[ToolCall]:
        return list(self._audit)

    def _next_id(self) -> str:
        return f"tc-{len(self._audit) + 1:04d}"

    @staticmethod
    def _key(name: str, arguments: dict[str, Any]) -> str:
        return f"{name}:{json.dumps(arguments, sort_keys=True, default=str)}"

    def _refuse(self, name: str, arguments: dict[str, Any], reason: str) -> ToolCall:
        call = ToolCall(id=self._next_id(), name=name, arguments=arguments, error=reason)
        self._audit.append(call)
        return call


class _BaseGateway(_GatewayPolicy):
    async def _invoke(self, spec: ToolSpec, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolCall:
        spec = self.allowed.get(name)
        if spec is None:
            return self._refuse(name, arguments, f"tool {name!r} is not in this agent's catalog")

        key = self._key(name, arguments)
        if (previous := self._seen.get(key)) is not None:
            replay = ToolCall(
                id=self._next_id(),
                name=name,
                arguments=arguments,
                result=previous.result,
                error=previous.error,
                replayed=True,
            )
            self._audit.append(replay)
            return replay

        if self.tracker is not None:
            self.tracker.record_tool_call()
            self.tracker.check()

        started = time.monotonic()
        call = ToolCall(id=self._next_id(), name=name, arguments=arguments)
        try:
            call.result = await asyncio.wait_for(
                self._invoke(spec, arguments), timeout=spec.timeout_seconds
            )
            # A tool that answers "no capacity for that" returns an error field
            # rather than raising — it is a business answer, not a crash. Recording
            # it as a success made `ok` mean "the transport worked", which is not
            # what any caller wants it to mean: the model saw a successful call
            # with no usable value in it and filled the gap itself.
            if isinstance(call.result, dict) and (message := call.result.get("error")):
                call.error = str(message)
        except TimeoutError:
            call.error = f"timed out after {spec.timeout_seconds}s"
        except Exception as exc:  # noqa: BLE001 - the audit row is the handling
            call.error = f"{type(exc).__name__}: {exc}"
        call.duration_ms = (time.monotonic() - started) * 1000

        self._audit.append(call)
        self._seen[key] = call
        return call


class InProcessGateway(_BaseGateway):
    """Calls the tool functions directly. Same policy, no subprocess."""

    def __init__(
        self,
        functions: dict[str, Any],
        allowed: dict[str, ToolSpec],
        tracker: BudgetTracker | None = None,
    ) -> None:
        super().__init__(allowed=allowed, tracker=tracker)
        self._functions = functions

    async def _invoke(self, spec: ToolSpec, arguments: dict[str, Any]) -> Any:
        function = self._functions.get(spec.name)
        if function is None:
            raise ToolUnavailable(f"{spec.name} has no implementation registered")
        # Off the event loop, or the timeout is decorative: a synchronous call
        # awaited directly blocks the loop, and `wait_for` never gets the chance
        # to cancel it. Cancelling a thread does not stop it — the work runs on
        # in the background — but the caller is freed, which is what a timeout
        # is for.
        return await asyncio.to_thread(lambda: function(**arguments))


class McpGateway(_BaseGateway):
    """Speaks MCP over stdio to real server processes.

    One session per server, opened once and held for the run. Opening a
    connection per call would make the audit's duration column mostly process
    startup, and would lose the protocol's own session semantics.
    """

    def __init__(
        self,
        servers: dict[str, list[str]],
        allowed: dict[str, ToolSpec],
        tracker: BudgetTracker | None = None,
    ) -> None:
        super().__init__(allowed=allowed, tracker=tracker)
        self._servers = servers
        self._sessions: dict[str, Any] = {}
        self._stack: Any = None

    async def __aenter__(self) -> McpGateway:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for name, argv in self._servers.items():
            params = StdioServerParameters(command=argv[0], args=argv[1:])
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[name] = session
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(*exc_info)
        self._sessions.clear()

    async def _invoke(self, spec: ToolSpec, arguments: dict[str, Any]) -> Any:
        session = self._sessions.get(spec.server)
        if session is None:
            raise ToolUnavailable(f"no session for server {spec.server!r}")

        result = await session.call_tool(spec.tool, arguments)
        if result.is_error:
            raise ToolUnavailable(_text_of(result))

        # Field names are snake_case in the 2.x SDK. `structured_content` is only
        # populated when a tool declares an output schema; ours return plain JSON
        # text, so the text block is the normal path rather than the fallback.
        structured = result.structured_content
        if structured is not None:
            # A bare list or scalar arrives wrapped under "result"; a dict arrives
            # as itself. Unwrapping here keeps a tool's shape identical on both
            # gateways, so an agent cannot tell which one it is talking to.
            return (
                structured.get("result", structured) if isinstance(structured, dict) else structured
            )

        text = _text_of(result)
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text


def _text_of(result: Any) -> str:
    blocks = getattr(result, "content", None) or []
    return "\n".join(getattr(block, "text", "") for block in blocks).strip()
