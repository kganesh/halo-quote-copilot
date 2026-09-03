"""A running record of what this project has spent on model calls.

Estimates, not an invoice — the rates in `bedrock.PRICE_PER_MTOK` are
first-party and Bedrock prices separately. What it is good for is the question
that actually comes up during a build: "how much has today cost me, and which
command is eating it".

One JSON object per line, appended and never rewritten, so a crashed run still
leaves its row behind.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from halo.platform.budget import Usage

LEDGER_PATH = Path(
    os.environ.get("HALO_LEDGER", Path(__file__).resolve().parents[3] / "data" / "spend.jsonl")
)


def record(command: str, model: str, usage: Usage, *, path: Path | None = None) -> None:
    """Append one run. Never raises — a ledger failure must not fail the run."""
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": command,
        "model": model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "tool_calls": usage.tool_calls,
        "usd": str(usage.usd),
    }
    target = path or LEDGER_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


@dataclass(frozen=True)
class Total:
    runs: int
    input_tokens: int
    output_tokens: int
    tool_calls: int
    usd: Decimal


def read(path: Path | None = None) -> list[dict]:
    target = path or LEDGER_PATH
    if not target.exists():
        return []
    entries = []
    for line in target.read_text().splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue  # a torn final line is not worth losing the rest over
    return entries


def totals(entries: list[dict]) -> Total:
    return Total(
        runs=len(entries),
        input_tokens=sum(e["input_tokens"] for e in entries),
        output_tokens=sum(e["output_tokens"] for e in entries),
        tool_calls=sum(e.get("tool_calls", 0) for e in entries),
        usd=sum((Decimal(e["usd"]) for e in entries), Decimal("0")),
    )


def by_command(entries: list[dict]) -> dict[str, Total]:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry["command"], []).append(entry)
    return {command: totals(rows) for command, rows in sorted(grouped.items())}
