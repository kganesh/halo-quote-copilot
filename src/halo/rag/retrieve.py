"""Hybrid retrieval: what the vectors find, and what the words find.

Both halves are needed and they fail differently. Semantic search knows that
"how long until it ships" is about lead time even with no shared words; it is
unreliable about *which* number you asked for. Lexical search nails `$22.00`,
`PMS` and `2XL`, and is helpless when the question shares no vocabulary with the
answer.

Fused with Reciprocal Rank Fusion — each result scores `1 / (k + rank)` in each
list, summed. RRF combines rankings rather than scores, which matters because a
BM25 score of 8.9 and a cosine of 0.71 are not on any common scale, and any
attempt to weight them directly is a fudge factor waiting to be tuned forever.
"""

from __future__ import annotations

from dataclasses import dataclass

from halo.domain.quote import Citation, CitationKind
from halo.rag.bm25 import Bm25Index
from halo.rag.chunk import Chunk
from halo.rag.embed import Embedder
from halo.rag.store import VectorStore

RRF_K = 60
"""Standard RRF damping. Large enough that the gap between rank 1 and rank 2
does not dominate, small enough that deep results still fade out."""


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: float
    vector_rank: int | None
    lexical_rank: int | None

    @property
    def found_by(self) -> str:
        """Which half surfaced it — the useful thing to see when a result looks
        wrong, and the reason both ranks are carried rather than just the score."""
        if self.vector_rank is not None and self.lexical_rank is not None:
            return "both"
        return "vector" if self.vector_rank is not None else "lexical"

    def as_citation(self) -> Citation:
        """A retrieval result becomes evidence a Quote can carry.

        `supporting_text` is the chunk itself, not a summary of it: the whole
        point of the citation check is that a reader — or a test — can look for
        the claim inside it.
        """
        return Citation(
            kind=CitationKind.CHUNK,
            ref=self.chunk.id,
            supporting_text=self.chunk.text,
        )


class AtlasRetriever:
    """Reads the store once, then answers questions against it."""

    def __init__(self, store: VectorStore, embedder: Embedder) -> None:
        self._store = store
        self._embedder = embedder
        chunks = store.all_chunks()
        self._by_id = {chunk.id: chunk for chunk in chunks}
        self._lexical = Bm25Index.build({c.id: c.embed_text for c in chunks})

    @property
    def size(self) -> int:
        return len(self._by_id)

    def search(self, query: str, limit: int = 5, pool: int = 15) -> list[ScoredChunk]:
        """Top `limit` chunks, fusing a `pool`-deep list from each half."""
        if not self._by_id:
            return []

        vector = self._embedder.embed([query])[0]
        vector_hits = self._store.search(vector, limit=pool)
        lexical_hits = self._lexical.search(query, limit=pool)

        vector_rank = {cid: rank for rank, (cid, _) in enumerate(vector_hits, start=1)}
        lexical_rank = {cid: rank for rank, (cid, _) in enumerate(lexical_hits, start=1)}

        fused: dict[str, float] = {}
        for ranks in (vector_rank, lexical_rank):
            for chunk_id, rank in ranks.items():
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)

        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
        return [
            ScoredChunk(
                chunk=self._by_id[chunk_id],
                score=score,
                vector_rank=vector_rank.get(chunk_id),
                lexical_rank=lexical_rank.get(chunk_id),
            )
            for chunk_id, score in ordered[:limit]
            if chunk_id in self._by_id
        ]
