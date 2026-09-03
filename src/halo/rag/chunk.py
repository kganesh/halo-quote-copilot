"""Split Atlas documents into citable pieces.

Chunked on markdown headings rather than a fixed token window. These documents
are already written in sections that each make one point — "Colour limits",
"Charges", "Lead time" — so the headings are a better boundary than any window
size, and the heading becomes the human-readable half of the citation.

A chunk id is `{doc_id}#{slug}`, which keeps the `atl-` prefix the `Quote`
validator requires of a `CitationKind.CHUNK` reference. That is not a
coincidence: an id that cannot be cited is not worth minting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MIN_CHUNK_WORDS = 12
"""Below this a section is merged into the one after it.

Short sections here are real facts — "10 business days after artwork approval"
is exactly what someone asks for. But a two-word chunk retrieves badly (BM25
length normalisation flatters very short documents) and reads as a fragment
when cited. Merging keeps the fact and gives it enough around it to be worth
pointing at.
"""

MAX_CHUNK_WORDS = 220
"""Above this a section is split on paragraph boundaries.

Titan v2 would happily embed far more, but a citation is only useful if a reader
can see the claim in it. A chunk the size of a page is a document reference
wearing a citation's clothes.
"""


@dataclass(frozen=True)
class Chunk:
    id: str
    doc_id: str
    doc_title: str
    heading: str
    text: str
    ordinal: int

    @property
    def embed_text(self) -> str:
        """What gets embedded: the heading carries context the body assumes.

        A section headed "Lead time" whose body says "Standard production is 7
        business days" is about lead time whether or not the words appear in the
        paragraph. Embedding the body alone loses that.
        """
        return f"{self.doc_title} — {self.heading}\n\n{self.text}"


def _slug(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "body"


def _split_long(text: str) -> list[str]:
    """Break an over-long section on blank lines, never mid-sentence."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    current: list[str] = []
    words = 0
    for paragraph in paragraphs:
        length = len(paragraph.split())
        if current and words + length > MAX_CHUNK_WORDS:
            pieces.append("\n\n".join(current))
            current, words = [], 0
        current.append(paragraph)
        words += length
    if current:
        pieces.append("\n\n".join(current))
    return pieces


def chunk_document(doc_id: str, title: str, body: str) -> list[Chunk]:
    """Split one Atlas document into chunks, in document order."""
    lines = body.splitlines()
    sections: list[tuple[str, list[str]]] = []
    heading = "Overview"
    buffer: list[str] = []

    for line in lines:
        if match := re.match(r"^(#{1,3})\s+(.*)$", line):
            level, text = len(match.group(1)), match.group(2).strip()
            if level == 1:
                # The H1 is the document title, already carried on every chunk.
                continue
            if buffer:
                sections.append((heading, buffer))
            heading, buffer = text, []
        else:
            buffer.append(line)
    if buffer:
        sections.append((heading, buffer))

    drafts: list[tuple[str, str]] = []
    for section_heading, section_lines in sections:
        text = "\n".join(section_lines).strip()
        if not text:
            continue
        pieces = _split_long(text) if len(text.split()) > MAX_CHUNK_WORDS else [text]
        for index, piece in enumerate(pieces):
            slug = _slug(section_heading)
            if len(pieces) > 1:
                slug = f"{slug}-{index + 1}"
            drafts.append((section_heading, piece) if not slug else (section_heading, piece))

    merged = _merge_short(drafts)

    return [
        Chunk(
            id=f"{doc_id}#{_slug(heading)}",
            doc_id=doc_id,
            doc_title=title,
            heading=heading,
            text=text,
            ordinal=ordinal,
        )
        for ordinal, (heading, text) in enumerate(merged)
    ]


def _merge_short(drafts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Fold undersized sections into the next one, keeping both headings.

    The merged chunk keeps the first heading as its own — that is what the
    citation shows — and carries the second inline so the fact under it is not
    orphaned from its label.
    """
    merged: list[tuple[str, str]] = []
    pending: tuple[str, str] | None = None

    for heading, text in drafts:
        if pending is not None:
            # The substantial half keeps its heading — that is what the citation
            # shows, and a two-line preamble should not take the id of the
            # section it sits above. The short text is folded in with its own
            # label so the fact under it is not orphaned.
            text = f"**{pending[0]}.** {pending[1]}\n\n{text}"
            pending = None
        if len(text.split()) < MIN_CHUNK_WORDS:
            pending = (heading, text)
            continue
        merged.append((heading, text))

    if pending is not None:
        if merged:
            last_heading, last_text = merged[-1]
            merged[-1] = (
                last_heading,
                f"{last_text}\n\n**{pending[0]}.** {pending[1]}",
            )
        else:
            merged.append(pending)
    return merged


def chunk_corpus(docs: list[dict]) -> list[Chunk]:
    """Chunk every Atlas document. `docs` is the seed `atlas_docs.json` shape."""
    chunks: list[Chunk] = []
    for doc in docs:
        chunks.extend(chunk_document(doc["id"], doc["title"], doc["body"]))
    return chunks
