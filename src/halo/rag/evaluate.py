"""Run the golden set and report where it fails.

Two rates, reported separately because they have different fixes. Retrieval
recall is a chunking and fusion problem; grounding is a prompt and verification
problem. A single blended score would hide which one is broken.
"""

from __future__ import annotations

from dataclasses import dataclass

from halo.agents.advisor import PolicyAnswer, answer_policy_question
from halo.platform.bedrock import ModelClient
from halo.platform.budget import BudgetTracker
from halo.platform.identity import Principal
from halo.platform.outcome import OutcomeStatus
from halo.rag.retrieve import AtlasRetriever


@dataclass
class GoldenResult:
    question: str
    expect_chunk: str
    retrieved: bool
    """The expected chunk reached the model."""
    cited: bool
    """The answer cited it."""
    fact_in_quote: bool
    """The cited quote contains the expected fact."""
    status: str
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.retrieved and self.cited and self.fact_in_quote


def run_golden(
    golden: list,
    *,
    principal: Principal,
    client: ModelClient,
    retriever: AtlasRetriever,
    tracker: BudgetTracker,
    limit: int = 6,
) -> list[GoldenResult]:
    results: list[GoldenResult] = []

    for case in golden:
        outcome, retrieved = answer_policy_question(
            case.question,
            principal=principal,
            client=client,
            retriever=retriever,
            tracker=tracker,
            limit=limit,
        )
        was_retrieved = any(hit.chunk.id == case.expect_chunk for hit in retrieved)

        cited = fact_ok = False
        detail = outcome.escalation_reason or ""
        if outcome.payload:
            answer = PolicyAnswer.model_validate(
                {k: v for k, v in outcome.payload.items() if k != "retrieved"}
            )
            matching = [f for f in answer.findings if f.chunk_id == case.expect_chunk]
            cited = bool(matching)
            fact_ok = any(case.expect_fact in f.quote for f in matching)
            if cited and not fact_ok:
                detail = f"cited but quote lacks {case.expect_fact!r}: " + "; ".join(
                    f.quote[:60] for f in matching
                )
            elif not cited and answer.findings:
                detail = "cited " + ", ".join(sorted({f.chunk_id for f in answer.findings}))

        results.append(
            GoldenResult(
                question=case.question,
                expect_chunk=case.expect_chunk,
                retrieved=was_retrieved,
                cited=cited,
                fact_in_quote=fact_ok,
                status=outcome.status.value,
                detail=detail,
            )
        )
    return results


def summarise(results: list[GoldenResult]) -> dict:
    total = len(results) or 1
    return {
        "total": len(results),
        "retrieval": sum(r.retrieved for r in results),
        "cited": sum(r.cited for r in results),
        "grounded": sum(r.passed for r in results),
        "completed": sum(r.status == OutcomeStatus.COMPLETED.value for r in results),
        "retrieval_rate": sum(r.retrieved for r in results) / total,
        "grounded_rate": sum(r.passed for r in results) / total,
    }
