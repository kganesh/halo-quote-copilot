"""The governance layer, tested without a transport under it."""

from decimal import Decimal

import pytest

from halo.platform.budget import Budget, BudgetExceeded, BudgetTracker
from halo.platform.gateway import InProcessGateway, ToolSpec


def a_gateway(tracker=None, **overrides):
    functions = {
        "svc.echo": lambda **kw: {"echoed": kw},
        "svc.boom": lambda: (_ for _ in ()).throw(RuntimeError("upstream is down")),
        "svc.slow": _slow,
        **overrides,
    }
    allowed = {
        "svc.echo": ToolSpec("svc.echo", timeout_seconds=5),
        "svc.boom": ToolSpec("svc.boom", timeout_seconds=5),
        "svc.slow": ToolSpec("svc.slow", timeout_seconds=0.01),
    }
    return InProcessGateway(functions, allowed, tracker=tracker)


def _slow():
    import time

    time.sleep(0.2)
    return {"done": True}


@pytest.mark.asyncio
async def test_an_allowed_tool_is_called_and_audited():
    gw = a_gateway()
    call = await gw.call("svc.echo", {"a": 1})

    assert call.ok
    assert call.id == "tc-0001"
    assert call.result == {"echoed": {"a": 1}}
    assert len(gw.audit) == 1


@pytest.mark.asyncio
async def test_a_tool_outside_the_catalog_never_reaches_the_transport():
    gw = a_gateway()
    call = await gw.call("svc.delete_everything", {})

    assert not call.ok
    assert "not in this agent's catalog" in call.error
    assert gw.audit == [call], "a refusal is still an audit row"


@pytest.mark.asyncio
async def test_the_same_call_twice_is_answered_once():
    gw = a_gateway()
    first = await gw.call("svc.echo", {"a": 1})
    second = await gw.call("svc.echo", {"a": 1})

    assert second.replayed
    assert second.result == first.result
    assert second.id != first.id, "a replay is still its own audit row"


@pytest.mark.asyncio
async def test_different_arguments_are_a_different_call():
    gw = a_gateway()
    await gw.call("svc.echo", {"a": 1})
    second = await gw.call("svc.echo", {"a": 2})
    assert not second.replayed


@pytest.mark.asyncio
async def test_a_failing_tool_is_recorded_not_raised():
    """An agent has to see the failure to escalate on it."""
    gw = a_gateway()
    call = await gw.call("svc.boom", {})

    assert not call.ok
    assert "upstream is down" in call.error


@pytest.mark.asyncio
async def test_a_slow_tool_is_cut_off_at_its_own_timeout():
    gw = a_gateway()
    call = await gw.call("svc.slow", {})

    assert not call.ok
    assert "timed out after 0.01s" in call.error


@pytest.mark.asyncio
async def test_tool_calls_count_against_the_budget():
    tracker = BudgetTracker(
        Budget(wall_clock_seconds=30, max_tokens=1000, max_tool_calls=1, max_usd=Decimal("1")),
        now=lambda: 0.0,
    )
    gw = a_gateway(tracker=tracker)

    await gw.call("svc.echo", {"a": 1})
    with pytest.raises(BudgetExceeded) as exc:
        await gw.call("svc.echo", {"a": 2})
    assert exc.value.dimension == "max_tool_calls"


@pytest.mark.asyncio
async def test_a_replay_does_not_spend_budget():
    """Charging twice for one answer would make the ceiling depend on how often
    the model repeats itself."""
    tracker = BudgetTracker(
        Budget(wall_clock_seconds=30, max_tokens=1000, max_tool_calls=1, max_usd=Decimal("1")),
        now=lambda: 0.0,
    )
    gw = a_gateway(tracker=tracker)

    await gw.call("svc.echo", {"a": 1})
    replay = await gw.call("svc.echo", {"a": 1})

    assert replay.replayed
    assert tracker.usage.tool_calls == 1


@pytest.mark.asyncio
async def test_a_tool_answering_with_an_error_field_is_not_a_success():
    """These tools report "no capacity for that" by returning an error field
    rather than raising. Counting that as ok made the model see a successful
    call with nothing usable in it, and fill the gap itself."""
    gw = a_gateway(**{"svc.refuses": lambda: {"error": "no capacity for 500 units"}})
    gw.allowed["svc.refuses"] = ToolSpec("svc.refuses", timeout_seconds=5)

    call = await gw.call("svc.refuses", {})

    assert not call.ok
    assert call.error == "no capacity for 500 units"
