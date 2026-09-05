"""The checks that fail a build, all of them offline.

M7's done-when is that a grounding regression fails the build rather than
reaching a demo. The obvious reading is "run the golden set in CI", and it does
not work: the golden set calls Bedrock, which needs credentials CI does not have
and spends money on every push.

So the question became which part of a grounding regression can be caught without
a model, and the answer is most of it. A grounding failure has three causes and
two of them are deterministic:

  retrieval   the chunk holding the answer stopped being findable. Chunking
              changed, a document was edited, an id moved. No model involved.
  fixture     the golden set says a fact lives in a chunk and it no longer does,
              so the case would fail live for a reason that has nothing to do
              with the model. This is the one that rots silently.
  answering   the model paraphrased instead of quoting. Only a live run sees it,
              and `halo eval` is where that lives.

The first two run here in about a second and cost nothing, over the real Atlas
corpus rather than a fixture of it. The third is the reason `halo eval` still
exists.

Retrieval is checked lexically. The vector half needs Titan and therefore a
network, so the gate asserts something narrower and honest: the expected chunk is
findable by BM25 alone. That is a weaker bar than the hybrid retriever clears in
production, which is what makes it a good gate — it fails on a real regression
and not on a ranking wobble.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from halo.agents.advisor import _normalise, verify
from halo.evals.atlas_golden import GOLDEN, GoldenQuestion
from halo.rag.bm25 import Bm25Index
from halo.rag.chunk import Chunk, chunk_corpus
from halo.seed.generate import build_atlas, build_suppliers

LEXICAL_DEPTH = 8
"""How far down the BM25 ranking the expected chunk may be.

The hybrid retriever supplies six excerpts and has a vector half helping it. This
gate has neither, so it allows a little more room. Tightening it to 1 would fail
on wording changes that production absorbs without noticing."""


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str = ""


def corpus() -> list[Chunk]:
    """The real Atlas documents, chunked the way ingest chunks them.

    Read from the generator rather than from `data/seed`, so the gate runs on a
    clean checkout with no `make seed` first. CI should not need a build step to
    tell you the corpus is broken.
    """
    documents = [doc.model_dump(mode="json") for doc in build_atlas(build_suppliers())]
    return chunk_corpus(documents)


def check_fixtures(golden: list[GoldenQuestion], chunks: list[Chunk]) -> list[GateResult]:
    """Every golden case's expected fact really is in its expected chunk.

    This is the check that catches the quiet rot. A document gets reworded, the
    fact moves to a neighbouring chunk, and nothing fails until someone runs the
    live eval and reads a 17/20 as a model regression.
    """
    by_id = {chunk.id: chunk for chunk in chunks}
    results = []
    for case in golden:
        chunk = by_id.get(case.expect_chunk)
        if chunk is None:
            results.append(
                GateResult(case.expect_chunk, False, "the expected chunk no longer exists")
            )
        elif _normalise(case.expect_fact) not in _normalise(chunk.text):
            results.append(
                GateResult(
                    case.expect_chunk,
                    False,
                    f"{case.expect_fact!r} is no longer in this chunk",
                )
            )
        else:
            results.append(GateResult(case.expect_chunk, True))
    return results


def check_retrieval(golden: list[GoldenQuestion], chunks: list[Chunk]) -> list[GateResult]:
    """Every golden question still finds its chunk with keyword search alone."""
    index = Bm25Index.build({chunk.id: chunk.embed_text for chunk in chunks})
    results = []
    for case in golden:
        hits = [chunk_id for chunk_id, _ in index.search(case.question, limit=LEXICAL_DEPTH)]
        if case.expect_chunk in hits:
            results.append(GateResult(case.question[:48], True))
        else:
            results.append(
                GateResult(
                    case.question[:48],
                    False,
                    f"{case.expect_chunk} is not in the top {LEXICAL_DEPTH}: {hits[:3]}",
                )
            )
    return results


def check_grounding_rejects_a_paraphrase(chunks: list[Chunk]) -> GateResult:
    """The verbatim check still refuses a paraphrase.

    A grounding regression is not only "the model paraphrased". It is also "the
    check stopped noticing", and that one is invisible in a live eval: the score
    goes up. Loosening `_normalise` to lowercase or strip punctuation would do
    it, and this fails when someone does.
    """
    from halo.agents.advisor import Finding, PolicyAnswer
    from halo.rag.retrieve import ScoredChunk

    chunk = next(c for c in chunks if c.id.startswith("atl-screen-print-standards"))
    sentence = chunk.text.split(".")[0].strip() + "."
    hit = ScoredChunk(chunk=chunk, score=1.0, vector_rank=1, lexical_rank=1)

    exact = PolicyAnswer(
        answer="quoted",
        findings=[Finding(claim="c", chunk_id=chunk.id, quote=sentence)],
    )
    paraphrase = PolicyAnswer(
        answer="paraphrased",
        findings=[
            Finding(claim="c", chunk_id=chunk.id, quote=sentence.replace(" ", "  ") + " ish")
        ],
    )

    if verify(exact, [hit]):
        return GateResult("grounding", False, "an exact quote was rejected")
    if not verify(paraphrase, [hit]):
        return GateResult("grounding", False, "a paraphrase was accepted")
    return GateResult("grounding", True)


def check_teardown() -> list[GateResult]:
    """Nothing in the stack survives `terraform destroy` or bills while idle.

    M8's done-when, in the half that does not need an account. The other half is
    `halo teardown --live`, which asks Cost Explorer the morning after.
    """
    from halo.infra import check

    findings = check()
    if not findings:
        return [GateResult("terraform", True)]
    return [GateResult(finding.resource, False, finding.problem) for finding in findings]


def check_corpus() -> GateResult | None:
    """A missing corpus is a setup problem, not twenty failing gates.

    The red-team set runs against the real tool servers, which read the generated
    seed. Without it every note fails somewhere inside a lambda, and the report
    blames the guardrail for a missing file.
    """
    from halo.mcp_servers.store import CorpusMissing, products

    try:
        products()
    except CorpusMissing as missing:
        return GateResult("corpus", False, str(missing))
    return None


async def run_all() -> dict[str, list[GateResult]]:
    """Every offline gate. The red-team set is one of them."""
    from halo.evals.redteam import run_offline

    if missing := check_corpus():
        return {"corpus": [missing]}

    chunks = corpus()
    redteam = [
        GateResult(result.note.id, result.passed, "" if result.passed else "obeyed")
        for result in await run_offline()
    ]
    return {
        "fixtures": check_fixtures(GOLDEN, chunks),
        "retrieval": check_retrieval(GOLDEN, chunks),
        "grounding": [check_grounding_rejects_a_paraphrase(chunks)],
        "redteam": redteam,
        "teardown": check_teardown(),
    }


def report(gates: dict[str, list[GateResult]]) -> str:
    lines = []
    for name, results in gates.items():
        failed = [result for result in results if not result.passed]
        mark = "PASS" if not failed else "FAIL"
        lines.append(f"[{mark}] {name:<10} {len(results) - len(failed)}/{len(results)}")
        for result in failed:
            lines.append(f"         {result.name}: {result.detail}")
    return "\n".join(lines)


def passed(gates: dict[str, list[GateResult]]) -> bool:
    return all(result.passed for results in gates.values() for result in results)


def write_report(gates: dict[str, list[GateResult]], path: Path) -> None:
    """The same report as a file, for a CI job summary."""
    path.write_text(report(gates) + "\n")
