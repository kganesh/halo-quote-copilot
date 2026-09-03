"""Chunking decides what a citation can point at."""

from halo.rag.chunk import MIN_CHUNK_WORDS, Chunk, chunk_document

DOC = """\
# Screen Print Standards

Intro paragraph that sits above any heading.

## Colour limits

A maximum of six spot colours may be printed in a single imprint location.
Each colour requires its own screen.

## Charges

- Screen setup: $22.00 per colour, per location.
- Run charge: $0.85 per piece for the first colour.

## Note

Short.
"""


def test_sections_become_chunks_with_citable_ids():
    chunks = chunk_document("atl-screen-print-standards", "Screen Print Standards", DOC)
    ids = [c.id for c in chunks]

    assert "atl-screen-print-standards#colour-limits" in ids
    assert "atl-screen-print-standards#charges" in ids


def test_every_id_keeps_the_prefix_a_chunk_citation_requires():
    """`Quote` rejects a CHUNK citation whose ref does not start with `atl-`."""
    chunks = chunk_document("atl-rush-policy", "Rush Production Policy", DOC)
    assert all(c.id.startswith("atl-") for c in chunks)
    assert all("#" in c.id for c in chunks)


def test_the_h1_is_not_a_section():
    """The H1 is the document title. Every chunk already carries it."""
    chunks = chunk_document("atl-x", "Screen Print Standards", DOC)
    assert all(c.heading != "Screen Print Standards" for c in chunks)
    assert all(c.doc_title == "Screen Print Standards" for c in chunks)


def test_an_undersized_section_is_merged_rather_than_left_as_a_fragment():
    chunks = chunk_document("atl-x", "Doc", DOC)
    assert all(len(c.text.split()) >= MIN_CHUNK_WORDS for c in chunks)
    assert any("Short." in c.text for c in chunks), "the fact itself is kept"


def test_a_merged_section_keeps_its_own_heading_inline():
    """Otherwise the fact loses the label that explains it."""
    chunks = chunk_document("atl-x", "Doc", DOC)
    merged = next(c for c in chunks if "Short." in c.text)
    assert "**Note.**" in merged.text


def test_a_short_preamble_does_not_steal_the_next_heading():
    """A two-line introduction should not take the id of the section below it.
    A citation to `#overview` for a colour limit points at the wrong label."""
    chunks = chunk_document("atl-screen-print-standards", "Screen Print Standards", DOC)
    ids = [c.id for c in chunks]

    assert "atl-screen-print-standards#colour-limits" in ids
    assert "atl-screen-print-standards#overview" not in ids

    limits = next(c for c in chunks if c.id.endswith("#colour-limits"))
    assert "six spot colours" in limits.text
    assert "Intro paragraph" in limits.text, "the preamble is kept, not dropped"


def test_embed_text_carries_the_heading():
    """A section titled "Lead time" is about lead time, whether or not those
    words appear in the body."""
    chunk = Chunk(
        id="atl-x#lead-time",
        doc_id="atl-x",
        doc_title="Doc",
        heading="Lead time",
        text="Standard production is 7 business days.",
        ordinal=0,
    )
    assert "Lead time" in chunk.embed_text
    assert "Doc" in chunk.embed_text
