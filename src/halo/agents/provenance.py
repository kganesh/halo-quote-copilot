"""Does this figure appear in the call it cites?

M2 answered that question for one agent. M6 has five, so the answer moved here.
The mechanism is unchanged and the wording of every problem is unchanged too,
because those strings end up in an escalation reason a human reads.

The important part is `hints`. Each figure names the fields it could legitimately
have come from, and only those fields are searched. Without that, any number
anywhere in the result matches: `estimate_freight` returns `{"freight_cost":
"294.00", "zone": 2}`, and a fabricated $2.00 freight charge once passed
verification by matching the zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from halo.platform.gateway import ToolCall


class SourcedFigure(BaseModel):
    """A number and the call it came from. Both parts are checked."""

    value: Decimal
    tool_call_id: str = Field(pattern=r"^tc-\d{4}$")


@dataclass(frozen=True)
class FigureCheck:
    """One reported figure, and where it says it came from."""

    label: str
    value: Decimal | date
    tool_call_id: str
    hints: tuple[str, ...]
    """Field-name fragments the value could legitimately live under."""


def values_under(result: Any, hints: tuple[str, ...]) -> set[str]:
    """Values stored under a key whose name matches one of `hints`.

    Returning an empty set is meaningful: it says no field in this result could
    have held this figure, which is a different failure from holding a different
    number.
    """
    found: set[str] = set()
    if isinstance(result, dict):
        for key, value in result.items():
            if any(hint in key.lower() for hint in hints):
                found |= _scalars(value)
            else:
                found |= values_under(value, hints)
    elif isinstance(result, list):
        for item in result:
            found |= values_under(item, hints)
    return found


def _scalars(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {s for v in value.values() for s in _scalars(v)}
    if isinstance(value, list):
        return {s for item in value for s in _scalars(item)}
    return {str(value)}


def matches(value: Decimal | date, result: Any, hints: tuple[str, ...]) -> bool:
    candidates = values_under(result, hints)
    if not candidates:
        return False
    if isinstance(value, date):
        return value.isoformat() in candidates
    for candidate in candidates:
        try:
            if Decimal(candidate) == value:
                return True
        except (InvalidOperation, ValueError):
            continue
    return False


def verify_figures(checks: list[FigureCheck], audit: list[ToolCall]) -> list[str]:
    """Problems with the provenance. Empty means every figure checks out."""
    by_id = {call.id: call for call in audit}
    problems: list[str] = []

    for check in checks:
        call = by_id.get(check.tool_call_id)
        if call is None:
            problems.append(f"{check.label} cites {check.tool_call_id}, which is not in the audit")
        elif not call.ok:
            problems.append(f"{check.label} cites {check.tool_call_id}, which failed: {call.error}")
        elif not matches(check.value, call.result, check.hints):
            problems.append(
                f"{check.label}={check.value} does not appear in {check.tool_call_id} ({call.name})"
            )

    return problems
