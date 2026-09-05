"""The governed event envelope: versioned, redacted, and durable.

A trace answers "what happened, in what order" and lives in a backend built for
sampling and expiry. This is the other record: every event kept whole, in a
bucket we control, for the auditor who asks in eleven months why a quote said
what it said.

Three properties, and each one is a decision that is expensive to add later:

**Versioned.** `schema_version` is on every event from the first one. A reader
in a year will meet events written by several versions of this code, and the
alternative to a version field is guessing from which keys are present.

**Redacted at the boundary.** An event is redacted when it is built, not when it
is read. A bucket is a thing that gets copied, granted to a data team, and
crawled by something nobody remembers enabling; "we redact on the way out" only
holds until the second reader. The patterns are the guardrail's own PII rules, so
what the guardrail blocks in an answer is what this removes from the record.

**Sinks are pluggable and boring.** `FileEventSink` writes JSONL beside the
ledger for a local run. `S3EventSink` puts one object per event under a
date-partitioned key, which is what Athena wants and what a lifecycle rule can
expire. Neither retries: a lost event is not a reason to fail a quote, and the
one thing worse than a missing event is a run that stopped to write one.

The bucket is created out of band; `docs/aws-setup.md` has it, with the
retention rule set in the same breath, because "forgotten logs, per GB-month,
forever" is on the cost list for a reason.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from halo.platform.guardrails import PII_PATTERNS

SCHEMA_VERSION = 1

EVENTS_PATH = Path(
    os.environ.get("HALO_EVENTS", Path(__file__).resolve().parents[3] / "data" / "events.jsonl")
)

REDACTED = "[redacted]"

SECRET_KEYS = re.compile(
    r"(?:password|secret|token|api[_-]?key|authorization|credential)", re.IGNORECASE
)
"""Keys whose value never travels, whatever it looks like.

A pattern catches a credential that looks like one. This catches the field that
is one — an empty `api_key` is still a field nobody should be able to read the
history of.
"""


@dataclass(frozen=True)
class Event:
    """One thing that happened, as it will be read years from now."""

    kind: str
    run_id: str
    agent: str
    attributes: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = None
    user_id: str | None = None
    schema_version: int = SCHEMA_VERSION
    event_id: str = ""
    at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id or f"evt-{uuid.uuid4().hex[:12]}",
            "at": self.at or datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": self.kind,
            "run_id": self.run_id,
            "agent": self.agent,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "attributes": redact(self.attributes),
        }


def redact(value: Any) -> Any:
    """Remove anything a record should not carry, at every depth.

    Keys are checked before values, so a secret whose format we do not recognise
    is still removed when it is stored under a name that says what it is.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if SECRET_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _scrub(value)
    return value


def _scrub(text: str) -> str:
    for pattern in PII_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class NullEventSink:
    """Writes nothing. The default, so an uninstrumented run costs nothing."""

    def emit(self, event: Event) -> None:
        return None


class FileEventSink:
    """One JSON object per line, appended. The local equivalent of the bucket."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or EVENTS_PATH

    def emit(self, event: Event) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as handle:
                handle.write(json.dumps(event.to_json(), default=str) + "\n")
        except OSError:
            # Same rule as the ledger. Losing an event is a small problem;
            # failing a quote because one could not be written is a larger one.
            pass


class S3EventSink:
    """One object per event, under a date-partitioned key.

    `kind=…/date=…/` rather than a flat prefix, because the first question asked
    of this bucket is always "what happened on the day of the complaint", and a
    partition answers it without scanning the year.
    """

    def __init__(self, bucket: str, *, prefix: str = "events", client: Any = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("s3")
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def key_for(self, payload: dict[str, Any]) -> str:
        day = str(payload["at"])[:10]
        return f"{self._prefix}/kind={payload['kind']}/date={day}/{payload['event_id']}.json"

    def emit(self, event: Event) -> None:
        payload = event.to_json()
        # A lost event must not fail a quote. The same rule as the ledger, and
        # the reason `put_object` is not retried: a bucket that is refusing
        # writes will still be refusing them on the retry, and the run is what
        # the seller is waiting for.
        with contextlib.suppress(Exception):
            self._client.put_object(
                Bucket=self._bucket,
                Key=self.key_for(payload),
                Body=json.dumps(payload, default=str).encode(),
                ContentType="application/json",
            )


def sink_from_env() -> EventSink:
    """The bucket when one is configured, a file when not, nothing when told."""
    if os.environ.get("HALO_EVENTS_DISABLED"):
        return NullEventSink()
    if bucket := os.environ.get("HALO_EVENTS_BUCKET"):
        return S3EventSink(bucket)
    return FileEventSink()
