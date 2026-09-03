"""The store and the fusion logic, using a deterministic fake embedder."""

import pytest

from halo.rag.chunk import Chunk
from halo.rag.retrieve import AtlasRetriever
from halo.rag.store import SqliteVectorStore, cosine


class FakeEmbedder:
    """Maps text to a vector by keyword, so relevance can be asserted exactly.

    A real embedder would make these tests a network call plus a judgement about
    similarity. This one makes them arithmetic.
    """

    AXES = ["screen", "embroidery", "freight", "rush"]

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def tokens_used(self) -> int:
        return 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.extend(texts)
        vectors = []
        for text in texts:
            lowered = text.lower()
            raw = [1.0 if axis in lowered else 0.0 for axis in self.AXES] or [0.0]
            magnitude = sum(v * v for v in raw) ** 0.5 or 1.0
            vectors.append([v / magnitude for v in raw])
        return vectors


def a_chunk(chunk_id: str, text: str, heading: str = "Charges") -> Chunk:
    return Chunk(
        id=chunk_id,
        doc_id=chunk_id.split("#")[0],
        doc_title="Doc",
        heading=heading,
        text=text,
        ordinal=0,
    )


CHUNKS = [
    a_chunk("atl-screen#charges", "Screen setup is $22.00 per colour, per location."),
    a_chunk("atl-embroidery#charges", "Embroidery digitizing is $65.00, one time."),
    a_chunk("atl-freight#zones", "Freight to zone 4 takes 3 business days in transit."),
    a_chunk("atl-rush#exclusions", "Rush is not available on four or more colours."),
]


@pytest.fixture
def store():
    store = SqliteVectorStore(":memory:")
    embedder = FakeEmbedder()
    store.upsert(CHUNKS, embedder.embed([c.embed_text for c in CHUNKS]))
    yield store
    store.close()


class TestStore:
    def test_a_chunk_round_trips(self, store):
        loaded = store.get("atl-screen#charges")
        assert loaded.text == CHUNKS[0].text
        assert loaded.heading == "Charges"

    def test_upsert_replaces_rather_than_duplicates(self, store):
        revised = a_chunk("atl-screen#charges", "Screen setup is now $25.00 per colour.")
        store.upsert([revised], FakeEmbedder().embed([revised.embed_text]))

        assert len(store.all_chunks()) == len(CHUNKS)
        assert "now $25.00" in store.get("atl-screen#charges").text

    def test_mismatched_vectors_are_refused(self, store):
        with pytest.raises(ValueError, match="2 chunks but 1 vectors"):
            store.upsert(CHUNKS[:2], [[0.0]])

    def test_an_unknown_chunk_is_none_not_an_error(self, store):
        assert store.get("atl-nothing#here") is None


def test_cosine_of_a_unit_vector_with_itself_is_one():
    """Titan normalises its vectors, so the dot product is the similarity."""
    vector = [0.6, 0.8]
    assert cosine(vector, vector) == pytest.approx(1.0)


class TestHybridFusion:
    def test_a_term_only_query_is_found_lexically(self, store):
        """`$65.00` matches none of this embedder's axes. Only BM25 finds it."""
        retriever = AtlasRetriever(store, FakeEmbedder())
        top = retriever.search("$65.00", limit=1)[0]

        assert top.chunk.id == "atl-embroidery#charges"
        assert top.lexical_rank is not None

    def test_both_halves_are_reported_when_both_find_it(self, store):
        retriever = AtlasRetriever(store, FakeEmbedder())
        top = retriever.search("rush", limit=1)[0]

        assert top.found_by == "both"
        assert top.vector_rank == 1

    def test_a_result_found_by_only_one_half_says_so(self, store):
        retriever = AtlasRetriever(store, FakeEmbedder())
        hits = {h.chunk.id: h for h in retriever.search("$22.00 screen", limit=4)}

        assert hits["atl-screen#charges"].found_by == "both"

    def test_an_empty_store_returns_nothing_rather_than_failing(self):
        empty = SqliteVectorStore(":memory:")
        retriever = AtlasRetriever(empty, FakeEmbedder())
        assert retriever.search("anything") == []
        empty.close()

    def test_a_retrieved_chunk_becomes_a_valid_citation(self, store):
        """The `Quote` validator rejects a CHUNK citation whose ref does not
        start with `atl-`. Retrieval must produce ids that pass that check."""
        retriever = AtlasRetriever(store, FakeEmbedder())
        citation = retriever.search("rush", limit=1)[0].as_citation()

        assert citation.kind.value == "chunk"
        assert citation.ref.startswith("atl-")
        assert citation.supporting_text == CHUNKS[3].text
