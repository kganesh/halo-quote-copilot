"""Hybrid retrieval: combining vector search and keyword search.

Both are needed, because they fail in different ways. Vector search understands
that "how long until it ships" is about lead time, even when no words match. It
is unreliable about which specific number you asked for. Keyword search finds
`$22.00`, `PMS` and `2XL` exactly. It finds nothing when the question and the
answer share no words.

The two result lists are combined with Reciprocal Rank Fusion. Each result
scores `1 / (k + rank)` in each list, and the scores are added.

RRF combines rankings, not scores. This matters because a BM25 score of 8.9 and
a cosine similarity of 0.71 are not on the same scale. Weighting them directly
would require a constant that has no principled value and would need retuning
whenever the corpus changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from halo.domain.quote import Citation, CitationKind
from halo.rag.bm25 import Bm25Index
from halo.rag.chunk import Chunk
from halo.rag.embed import Embedder
from halo.rag.store import VectorStore

RRF_K = 60
"""The standard RRF constant.

It is large enough that the gap between rank 1 and rank 2 does not dominate the
result. It is small enough that low-ranked results still score close to zero.
"""


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: float
    vector_rank: int | None
    lexical_rank: int | None

    @property
    def found_by(self) -> str:
        """Which search found this result.

        This is the useful thing to see when a result looks wrong. It is why the
        object carries both ranks and not only the combined score.
        """
        if self.vector_rank is not None and self.lexical_rank is not None:
            return "both"
        return "vector" if self.vector_rank is not None else "lexical"

    def as_citation(self) -> Citation:
        """Convert a retrieval result into evidence a Quote can carry.

        `supporting_text` is the chunk itself, not a summary. The citation check
        works by looking for the claim inside this text. A reader can do the
        same.
        """
        return Citation(
            kind=CitationKind.CHUNK,
            ref=self.chunk.id,
            supporting_text=self.chunk.text,
        )


class AtlasRetriever:
    """Reads the store once, then answers questions from it."""

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
        """Return the top `limit` chunks, combining `pool` results from each
        search."""
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
