"""A running record of what this project has spent on model calls.

These are estimates, not an invoice. The rates come from
`bedrock.PRICE_PER_MTOK`, read from the Bedrock rate card.

The ledger answers the question that comes up during a build: how much has today
cost, and which command is spending it.

The file holds one JSON object per line. Rows are appended and never rewritten,
so a run that crashes still leaves its row on disk.
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
    """Append one run.

    This never raises an exception. Losing a spend record is a small problem.
    Failing the run because the record could not be written is a larger one.
    """
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": command,
        "model": model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
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
    cache_read_tokens: int
    cache_write_tokens: int
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
            continue  # A partly written final line should not discard the rest.
    return entries


def totals(entries: list[dict]) -> Total:
    # `.get` with a default on the cache fields, because rows written before
    # cache accounting existed do not have them and are still worth counting.
    return Total(
        runs=len(entries),
        input_tokens=sum(e["input_tokens"] for e in entries),
        output_tokens=sum(e["output_tokens"] for e in entries),
        cache_read_tokens=sum(e.get("cache_read_tokens", 0) for e in entries),
        cache_write_tokens=sum(e.get("cache_write_tokens", 0) for e in entries),
        tool_calls=sum(e.get("tool_calls", 0) for e in entries),
        usd=sum((Decimal(e["usd"]) for e in entries), Decimal("0")),
    )


def by_command(entries: list[dict]) -> dict[str, Total]:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry["command"], []).append(entry)
    return {command: totals(rows) for command, rows in sorted(grouped.items())}
