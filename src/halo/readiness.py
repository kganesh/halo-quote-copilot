"""What this deployment is actually running, and whether that is allowed here.

`halo doctor` answers "can I reach Bedrock". This answers a different question:
of the things that quietly degrade when they are not configured, which are
degraded right now, and does that matter at this stage?

The problem it exists for is not reachability. It is that every optional
dependency in this codebase falls back silently and correctly. No guardrail id
means the local pattern matcher. No bucket means a file beside the ledger. No
claims means the development seller. Each of those is right on a laptop and
alarming in production, and nothing anywhere says which one you are running.

So a probe reports what it *found*, and the stage decides whether that is a
failure. The table below is the deployment contract, and writing it down is most
of the value here — until now it existed only as scattered `or` expressions.

  local         everything may fall back. This is a laptop.
  integration   the managed services must be real, because the point of the
                environment is to exercise them.
  production    integration, plus durable state and a verified token issuer.

**No AWS calls.** Nothing here costs anything or needs credentials, so it can run
on every deploy, in CI, and as a readiness endpoint without a bill. Reachability
is `halo doctor`, which makes one real model call and therefore cannot be polled.
Cost Explorer is deliberately not probed either: that API bills $0.01 a request,
which is a strange thing to pay on a health check.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Stage(StrEnum):
    LOCAL = "local"
    INTEGRATION = "integration"
    PRODUCTION = "production"


class State(StrEnum):
    """What a probe found, independent of whether it is acceptable."""

    OK = "ok"
    """The real thing is configured."""
    FALLBACK = "fallback"
    """A working substitute is in use. Fine somewhere, not everywhere."""
    MISSING = "missing"
    """Nothing is configured and there is no substitute."""


@dataclass(frozen=True)
class ProbeResult:
    name: str
    state: State
    detail: str
    fix: str | None = None


@dataclass(frozen=True)
class Probe:
    name: str
    run: Callable[[], ProbeResult]
    strict_from: frozenset[Stage]
    """Stages where anything short of `ok` is a failure.

    A probe absent from this set may fall back at that stage and still pass. The
    set is the interesting half of every row: `local` is empty for most probes,
    which is exactly the list of things that must become failures elsewhere.
    """


ALL_STAGES = frozenset(Stage)
DEPLOYED = frozenset({Stage.INTEGRATION, Stage.PRODUCTION})
PRODUCTION_ONLY = frozenset({Stage.PRODUCTION})


def probe_corpus() -> ProbeResult:
    from halo.mcp_servers.store import CorpusMissing, products

    try:
        rows = products()
    except CorpusMissing as missing:
        return ProbeResult("corpus", State.MISSING, str(missing), "make seed")
    return ProbeResult("corpus", State.OK, f"{len(rows)} products")


def probe_atlas_index() -> ProbeResult:
    from halo.rag.store import DEFAULT_DB

    path = Path(DEFAULT_DB)
    if not path.exists():
        return ProbeResult(
            "atlas index", State.MISSING, f"no index at {path}", "make ingest (~$0.0001)"
        )
    return ProbeResult("atlas index", State.OK, f"{path.stat().st_size // 1024} KB")


def probe_guardrail() -> ProbeResult:
    """The one most worth having. A local pattern set answering for a managed
    guardrail is the failure nobody would notice from the outside."""
    if guardrail_id := os.environ.get("HALO_GUARDRAIL_ID"):
        version = os.environ.get("HALO_GUARDRAIL_VERSION", "DRAFT")
        return ProbeResult("guardrail", State.OK, f"bedrock {guardrail_id} ({version})")
    return ProbeResult(
        "guardrail",
        State.FALLBACK,
        "local pattern set — a floor, not the managed policy",
        "export HALO_GUARDRAIL_ID (docs/aws-setup.md section 6)",
    )


def probe_events() -> ProbeResult:
    if bucket := os.environ.get("HALO_EVENTS_BUCKET"):
        return ProbeResult("events", State.OK, f"s3://{bucket}/events/")
    from halo.platform.events import EVENTS_PATH

    return ProbeResult(
        "events",
        State.FALLBACK,
        f"local file {EVENTS_PATH.name} — one copy, on this disk",
        "export HALO_EVENTS_BUCKET (docs/aws-setup.md section 8)",
    )


def probe_checkpoints() -> ProbeResult:
    """Durability, not writability.

    A file store works and survives nothing: two processes approving at once is
    a lost update, and a container's disk goes away with the container. There is
    no durable implementation yet, so production cannot pass this — which is the
    honest answer rather than a check tuned to give the one we want.
    """
    from halo.platform.checkpoint import CHECKPOINT_DIR

    return ProbeResult(
        "checkpoints",
        State.FALLBACK,
        f"files in {CHECKPOINT_DIR.name}/ — not durable, not shared",
        "no durable CheckpointStore exists yet; the Protocol is the seam for one",
    )


def probe_identity() -> ProbeResult:
    issuer = os.environ.get("HALO_COGNITO_ISSUER")
    audience = os.environ.get("HALO_COGNITO_AUDIENCE")
    if issuer and audience:
        return ProbeResult("identity", State.OK, f"claims verified against {issuer[-28:]}")
    if issuer or audience:
        return ProbeResult(
            "identity",
            State.MISSING,
            "half configured: issuer and audience are checked together or not at all",
            "set both HALO_COGNITO_ISSUER and HALO_COGNITO_AUDIENCE",
        )
    return ProbeResult(
        "identity",
        State.FALLBACK,
        "development claims, issuer unchecked",
        "export HALO_COGNITO_ISSUER and HALO_COGNITO_AUDIENCE (section 7)",
    )


def probe_model_priced() -> ProbeResult:
    """A model with no rate card entry makes every dollar budget unenforceable."""
    from halo.platform.bedrock import DEFAULT_MODEL, is_priced

    model = os.environ.get("HALO_MODEL", DEFAULT_MODEL)
    if is_priced(model):
        return ProbeResult("model priced", State.OK, model)
    return ProbeResult(
        "model priced",
        State.MISSING,
        f"{model} has no rate card entry, so max_usd cannot stop a run",
        "add it to PRICE_PER_MTOK in platform/bedrock.py",
    )


PROBES: tuple[Probe, ...] = (
    Probe("corpus", probe_corpus, ALL_STAGES),
    Probe("model priced", probe_model_priced, ALL_STAGES),
    Probe("atlas index", probe_atlas_index, DEPLOYED),
    Probe("guardrail", probe_guardrail, DEPLOYED),
    Probe("events", probe_events, DEPLOYED),
    Probe("identity", probe_identity, PRODUCTION_ONLY),
    Probe("checkpoints", probe_checkpoints, PRODUCTION_ONLY),
)


@dataclass(frozen=True)
class Assessment:
    probe: Probe
    result: ProbeResult
    stage: Stage

    @property
    def strict(self) -> bool:
        return self.stage in self.probe.strict_from

    @property
    def passed(self) -> bool:
        return self.result.state is State.OK or not self.strict


def run(stage: Stage) -> tuple[list[Assessment], bool]:
    """Every probe, always. Unlike `doctor`, this does not stop at the first
    failure: a readiness report is a list of what to fix, and stopping early
    hides the second thing from whoever is fixing the first."""
    assessments = [Assessment(probe, probe.run(), stage) for probe in PROBES]
    return assessments, all(a.passed for a in assessments)


MARKS = {State.OK: "OK  ", State.FALLBACK: "SUB ", State.MISSING: "GONE"}


def report(assessments: list[Assessment], ok: bool, stage: Stage) -> str:
    width = max(len(a.result.name) for a in assessments)
    lines = [f"stage {stage}", ""]

    for assessment in assessments:
        result = assessment.result
        mark = MARKS[result.state]
        flag = " " if assessment.passed else "!"
        lines.append(f" {flag}[{mark}] {result.name.ljust(width)}  {result.detail}")
        if not assessment.passed and result.fix:
            lines.append(f"         {' ' * width}  -> {result.fix}")

    substituted = [a for a in assessments if a.result.state is not State.OK and a.passed]
    lines.append("")
    if ok:
        lines.append(f"Ready for {stage}.")
        if substituted:
            lines.append(
                "Running on substitutes, which this stage allows: "
                + ", ".join(a.result.name for a in substituted)
                + "."
            )
    else:
        failed = [a.result.name for a in assessments if not a.passed]
        lines.append(f"Not ready for {stage}: {', '.join(failed)}.")
    return "\n".join(lines)
