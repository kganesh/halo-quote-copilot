"""Every tool call an agent makes goes through this module.

The reference architecture places a filtered catalog, typed schemas, timeouts,
idempotency and audit between the agents and the enterprise systems. This module
is that layer. It does four things:

- **Allow-list.** An agent can call only the tools its role was granted. A tool
  that is not on the list is refused before the transport is used.
- **Timeout.** Set per tool. A catalogue search and a capacity scan need
  different limits.
- **Idempotency.** The same call with the same arguments inside one run returns
  the recorded result. If the model asks twice, the tool runs once and the audit
  gets one row for the real call and one for the replay.
- **Audit.** Every call gets a `tool_call_id`. That id is what a quote cites, so
  provenance comes from the governance layer instead of being a separate feature.

There are two implementations. They share all the logic above and differ only in
how they reach the tool. `McpGateway` speaks MCP over stdio to real server
processes. `InProcessGateway` calls the same Python functions directly. Tests use
`InProcessGateway`, so the policy is tested without starting a subprocess for
each assertion. One integration test covers `McpGateway`.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from halo.platform import telemetry
from halo.platform.budget import BudgetTracker
from halo.platform.identity import Principal


@dataclass(frozen=True)
class ToolSpec:
    """One entry in the filtered catalog."""

    name: str
    """Written as `server.tool`, so an audit row shows which system answered."""
    timeout_seconds: float = 10.0
    scoped: bool = False
    """Whether this tool answers per-account and therefore needs an identity.

    A scoped tool is called with the gateway's principal attached. An unscoped
    one is not: a price is a price whoever asks, and passing identity to a tool
    that cannot use it would make every audit row look like an access decision.
    """

    @property
    def server(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def tool(self) -> str:
        return self.name.split(".", 1)[1]


@dataclass
class ToolCall:
    """The audit record. This is also what a quote cites.

    `result` stores the whole tool response, not a summary. At M7 this record is
    what goes into the evidence store. A summary written now would probably not
    contain what M7 needs.
    """

    id: str
    name: str
    arguments: dict[str, Any]
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    replayed: bool = False
    as_principal: str | None = None
    """The user this call was made as, on a scoped tool. `None` on the rest.

    The principal is recorded here rather than merged into `arguments`, so the
    audit keeps saying what the model asked for and says separately who it was
    asked as. Those are different facts and an investigation needs both."""

    @property
    def ok(self) -> bool:
        return self.error is None


class ToolUnavailable(Exception):
    """The tool exists and was allowed, but the call could not be completed.

    This is different from a refusal. A refusal is a policy decision. An
    unavailable tool is an outage. The agent must escalate in both cases, but the
    two need different fixes.
    """


class ToolGateway(Protocol):
    async def call(self, name: str, arguments: dict[str, Any]) -> ToolCall: ...
    @property
    def audit(self) -> list[ToolCall]: ...


@dataclass
class _GatewayPolicy:
    """The parts of the gateway that do not depend on the transport."""

    allowed: dict[str, ToolSpec]
    tracker: BudgetTracker | None = None
    principal: Principal | None = None
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

        # Identity is attached here or the call does not happen. The model never
        # supplies it: a `principal` argument in a tool_use block is the model
        # asking to choose who it is, which is refused rather than overwritten,
        # because silently correcting it would hide the attempt.
        if spec.scoped:
            if "principal" in arguments:
                return self._refuse(name, arguments, "identity is not an argument a caller may set")
            if self.principal is None:
                return self._refuse(
                    name, arguments, f"tool {name!r} is scoped and this gateway has no principal"
                )

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
        call = ToolCall(
            id=self._next_id(),
            name=name,
            arguments=arguments,
            as_principal=self.principal.user_id if spec.scoped and self.principal else None,
        )
        # The span carries the call id and not the arguments. The id is what a
        # quote cites, so it is the join between a trace and the evidence; the
        # arguments can hold an account id, a destination and a quantity, which
        # belong in the event record rather than in a tracing backend.
        span = telemetry.span(
            telemetry.TOOL,
            name,
            tool_call_id=call.id,
            scoped=spec.scoped,
            as_principal=call.as_principal,
        )
        invocation = (
            {**arguments, "principal": self.principal.model_dump(mode="json")}
            if spec.scoped and self.principal is not None
            else arguments
        )
        with span as current:
            await self._run(spec, call, invocation)
            current.set_attribute("halo.ok", call.ok)
            if call.error:
                current.set_attribute("halo.error", call.error)

        call.duration_ms = (time.monotonic() - started) * 1000
        self._audit.append(call)
        self._seen[key] = call
        return call

    async def _run(self, spec: ToolSpec, call: ToolCall, invocation: dict[str, Any]) -> None:
        """Invoke the tool and record the answer on the call, error included."""
        try:
            call.result = await asyncio.wait_for(
                self._invoke(spec, invocation), timeout=spec.timeout_seconds
            )
            # A tool that answers "no capacity for that" returns an error field
            # instead of raising an exception. That is a business answer, not a
            # crash. Recording it as a success made `ok` mean "the transport
            # worked". No caller wants that meaning. The model saw a successful
            # call with no usable value in it, and invented the value itself.
            if isinstance(call.result, dict) and (message := call.result.get("error")):
                call.error = str(message)
        except TimeoutError:
            call.error = f"timed out after {spec.timeout_seconds}s"
        except Exception as exc:  # noqa: BLE001 - the audit row is the handling
            call.error = f"{type(exc).__name__}: {exc}"


class InProcessGateway(_BaseGateway):
    """Calls the tool functions directly. Same policy, no subprocess."""

    def __init__(
        self,
        functions: dict[str, Any],
        allowed: dict[str, ToolSpec],
        tracker: BudgetTracker | None = None,
        principal: Principal | None = None,
    ) -> None:
        super().__init__(allowed=allowed, tracker=tracker, principal=principal)
        self._functions = functions

    async def _invoke(self, spec: ToolSpec, arguments: dict[str, Any]) -> Any:
        function = self._functions.get(spec.name)
        if function is None:
            raise ToolUnavailable(f"{spec.name} has no implementation registered")
        # Run this off the event loop, or the timeout does nothing. A
        # synchronous call awaited directly blocks the loop, so `wait_for` never
        # gets a chance to cancel it. Cancelling a thread does not stop the
        # thread. The work continues in the background. But the caller is freed,
        # and that is what a timeout is for.
        return await asyncio.to_thread(lambda: function(**arguments))


class McpGateway(_BaseGateway):
    """Speaks MCP over stdio to real server processes.

    One session per server, opened once and held for the whole run. Opening a
    connection per call would make the audit's duration column mostly measure
    process startup. It would also lose the session semantics that the MCP
    protocol defines.
    """

    def __init__(
        self,
        servers: dict[str, list[str]],
        allowed: dict[str, ToolSpec],
        tracker: BudgetTracker | None = None,
        principal: Principal | None = None,
    ) -> None:
        super().__init__(allowed=allowed, tracker=tracker, principal=principal)
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

        # Field names are snake_case in the 2.x SDK. `structured_content` is set
        # only when a tool declares an output schema. Our tools return plain JSON
        # text, so reading the text block is the normal path here, not a
        # fallback.
        structured = result.structured_content
        if structured is not None:
            # A bare list or scalar arrives wrapped under "result". A dict
            # arrives as itself. Unwrapping here keeps a tool's response shape
            # identical on both gateways, so an agent cannot tell which gateway
            # it is using.
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
