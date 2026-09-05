"""Design rule 06: approval resumes, it doesn't restart.

A quote below the margin floor stops and waits for a human. The easy version of
that is to remember the request, and re-run everything once someone says yes.
It is easy because it hides every state bug: the second run calls the tools
again, gets slightly different answers, and the thing the manager approved is
not the thing that gets sent. It also pays for the whole run twice.

So a checkpoint holds the *evidence*, not the question. Every specialist report,
every tool call those reports cite, the principal the work was done as, and the
margin that stopped it. Resuming reads that back and assembles the quote. No
model call, no tool call — and a test asserts exactly that by resuming with a
client that raises if it is touched.

Two decisions worth naming:

**The principal is stored and restored.** The work was done inside one seller's
scope and it stays there. An approver unlocks the quote; they do not take it
over, and nothing is re-fetched as them.

**Files, one JSON object per checkpoint.** Same tradeoff as the ledger, and the
same answer for a practice build: a real deployment wants a table, because two
processes approving at once is a lost update, and a checkpoint that outlives the
container is the entire point. `CheckpointStore` is the seam for that.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from halo.platform.identity import Principal, Role

CHECKPOINT_DIR = Path(
    os.environ.get("HALO_CHECKPOINTS", Path(__file__).resolve().parents[3] / "data" / "checkpoints")
)


class ApprovalError(Exception):
    """The checkpoint cannot be approved by this principal, or at all."""


@dataclass(frozen=True)
class Checkpoint:
    """A paused run, with everything needed to finish it and nothing else."""

    id: str
    created_at: str
    reason: str
    request: dict[str, Any]
    principal: dict[str, Any]
    reports: dict[str, Any]
    """One entry per specialist, keyed by name: the report it produced."""
    calls: list[dict[str, Any]]
    """The tool calls the reports cite, flattened. Citations resolve against these."""
    margin_pct: str
    floor_pct: str
    usage: dict[str, Any]
    approved_by: str | None = None
    approved_at: str | None = None

    @property
    def open(self) -> bool:
        return self.approved_by is None

    def owner(self) -> Principal:
        """The principal the work was done as, and will resume as."""
        return Principal.model_validate(self.principal)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "reason": self.reason,
            "request": self.request,
            "principal": self.principal,
            "reports": self.reports,
            "calls": self.calls,
            "margin_pct": self.margin_pct,
            "floor_pct": self.floor_pct,
            "usage": self.usage,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


class CheckpointStore(Protocol):
    def save(self, checkpoint: Checkpoint) -> None: ...
    def load(self, checkpoint_id: str) -> Checkpoint: ...
    def open_checkpoints(self) -> list[Checkpoint]: ...


class FileCheckpointStore:
    """One file per checkpoint, named by id."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or CHECKPOINT_DIR

    def save(self, checkpoint: Checkpoint) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{checkpoint.id}.json"
        # Written whole and renamed, so a crash mid-write cannot leave a
        # half-parsed checkpoint that resume would read as missing evidence.
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(checkpoint.to_json(), indent=2, default=str))
        temporary.replace(path)

    def load(self, checkpoint_id: str) -> Checkpoint:
        path = self._dir / f"{checkpoint_id}.json"
        if not path.exists():
            raise ApprovalError(f"no checkpoint {checkpoint_id}")
        return Checkpoint(**json.loads(path.read_text()))

    def open_checkpoints(self) -> list[Checkpoint]:
        if not self._dir.exists():
            return []
        found = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                checkpoint = Checkpoint(**json.loads(path.read_text()))
            except (ValueError, TypeError):
                continue  # A file we cannot read is not a reason to hide the rest.
            if checkpoint.open:
                found.append(checkpoint)
        return found


def new_id() -> str:
    return f"chk-{uuid.uuid4().hex[:10]}"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def may_approve(checkpoint: Checkpoint, approver: Principal) -> None:
    """Raise unless this approver may release this checkpoint.

    A seller cannot approve their own margin exception — that is the entire
    reason the gate exists. The rule is role and tenant, both checked here rather
    than in the CLI, so a second entry point cannot forget one.
    """
    if not checkpoint.open:
        raise ApprovalError(f"{checkpoint.id} was already approved by {checkpoint.approved_by}")

    owner = checkpoint.owner()
    if approver.tenant_id != owner.tenant_id:
        raise ApprovalError("an approver from another tenant cannot release this checkpoint")
    if approver.user_id == owner.user_id:
        raise ApprovalError("a quote cannot be approved by the seller who raised it")
    if approver.role is not Role.SALES_MANAGER:
        raise ApprovalError(f"role {approver.role} cannot approve a margin exception")


def approve(checkpoint: Checkpoint, approver: Principal) -> Checkpoint:
    """Mark a checkpoint released. Raises if this approver may not."""
    may_approve(checkpoint, approver)
    return Checkpoint(
        **{**checkpoint.to_json(), "approved_by": approver.user_id, "approved_at": now()}
    )


def money(value: object) -> Decimal:
    return Decimal(str(value))
