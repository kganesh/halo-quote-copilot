"""Where chunks and their vectors live.

A protocol with one implementation today. `SqliteVectorStore` keeps the corpus
in a single file and computes cosine similarity in Python — which sounds
inadequate until you count: 80 chunks of 1,024 dimensions is 80,000
multiply-adds per query, microseconds of work. At this size a vector database
would be infrastructure to maintain in exchange for nothing.

The protocol exists because that stops being true. When the corpus is large
enough for the arithmetic to matter, a `PgVectorStore` implements this same
three methods and nothing above it changes.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from typing import Protocol

from halo.rag.chunk import Chunk

DEFAULT_DB = Path(__file__).resolve().parents[3] / "data" / "atlas.db"


class VectorStore(Protocol):
    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...
    def search(self, vector: list[float], limit: int) -> list[tuple[str, float]]: ...
    def get(self, chunk_id: str) -> Chunk | None: ...
    def all_chunks(self) -> list[Chunk]: ...


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    """A plain dot product: Titan returns unit-length vectors, so the magnitudes
    are already 1 and dividing by them would be arithmetic with no effect."""
    return sum(x * y for x, y in zip(a, b, strict=True))


class SqliteVectorStore:
    """Chunks, metadata and vectors in one file. `:memory:` for tests."""

    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id         TEXT PRIMARY KEY,
                doc_id     TEXT NOT NULL,
                doc_title  TEXT NOT NULL,
                heading    TEXT NOT NULL,
                text       TEXT NOT NULL,
                ordinal    INTEGER NOT NULL,
                vector     BLOB NOT NULL
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        self._db.executemany(
            """
            INSERT INTO chunks (id, doc_id, doc_title, heading, text, ordinal, vector)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                doc_id=excluded.doc_id, doc_title=excluded.doc_title,
                heading=excluded.heading, text=excluded.text,
                ordinal=excluded.ordinal, vector=excluded.vector
            """,
            [
                (c.id, c.doc_id, c.doc_title, c.heading, c.text, c.ordinal, _pack(v))
                for c, v in zip(chunks, vectors, strict=True)
            ],
        )
        self._db.commit()

    def search(self, vector: list[float], limit: int = 10) -> list[tuple[str, float]]:
        rows = self._db.execute("SELECT id, vector FROM chunks").fetchall()
        scored = [(chunk_id, cosine(vector, _unpack(blob))) for chunk_id, blob in rows]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:limit]

    def _row_to_chunk(self, row: tuple) -> Chunk:
        return Chunk(
            id=row[0],
            doc_id=row[1],
            doc_title=row[2],
            heading=row[3],
            text=row[4],
            ordinal=row[5],
        )

    def get(self, chunk_id: str) -> Chunk | None:
        row = self._db.execute(
            "SELECT id, doc_id, doc_title, heading, text, ordinal FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        return self._row_to_chunk(row) if row else None

    def all_chunks(self) -> list[Chunk]:
        rows = self._db.execute(
            "SELECT id, doc_id, doc_title, heading, text, ordinal FROM chunks ORDER BY id"
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def stats(self) -> dict:
        count, docs = self._db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT doc_id) FROM chunks"
        ).fetchone()
        return {"chunks": count, "documents": docs, "path": self.path}
