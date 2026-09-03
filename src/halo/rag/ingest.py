"""Build the Atlas index: chunk, embed, store.

This re-embeds the whole corpus on every run instead of tracking which documents
changed. At 80 chunks and $0.02 per million tokens, a full rebuild costs a small
fraction of a cent. Tracking changes would be more code than it saves, and a
stale index is a failure that is hard to notice.
"""

from __future__ import annotations

import json
from pathlib import Path

from halo.rag.chunk import chunk_corpus
from halo.rag.embed import Embedder, TitanEmbedder
from halo.rag.store import DEFAULT_DB, SqliteVectorStore, VectorStore

SEED_ATLAS = Path(__file__).resolve().parents[3] / "data" / "seed" / "atlas_docs.json"


def ingest(
    store: VectorStore | None = None,
    embedder: Embedder | None = None,
    atlas_path: Path = SEED_ATLAS,
) -> dict:
    if not atlas_path.exists():
        raise FileNotFoundError(f"no Atlas corpus at {atlas_path} — run `make seed` first")

    docs = json.loads(atlas_path.read_text())
    chunks = chunk_corpus(docs)

    store = store or SqliteVectorStore(DEFAULT_DB)
    embedder = embedder or TitanEmbedder()

    vectors = embedder.embed([chunk.embed_text for chunk in chunks])
    store.upsert(chunks, vectors)

    return {
        "documents": len(docs),
        "chunks": len(chunks),
        "tokens": embedder.tokens_used,
        "usd": str(getattr(embedder, "usd", "0")),
    }


def main() -> None:
    summary = ingest()
    print(
        f"ingested {summary['documents']} documents -> {summary['chunks']} chunks "
        f"({summary['tokens']:,} tokens, ${summary['usd']})"
    )


if __name__ == "__main__":
    main()
