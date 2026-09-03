"""Split Atlas documents into pieces that can be cited.

We split on markdown headings, not on a fixed token count. These documents are
already written in sections that each make one point: "Colour limits",
"Charges", "Lead time". The headings are better boundaries than any window size.
The heading also becomes the readable part of the citation.

A chunk id has the format `{doc_id}#{slug}`. This keeps the `atl-` prefix that
the `Quote` validator requires for a `CitationKind.CHUNK` reference. That is
deliberate. An id that cannot be used in a citation is not useful here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MIN_CHUNK_WORDS = 12
"""A section shorter than this is merged into the section after it.

Short sections here contain real facts. "10 business days after artwork
approval" is exactly what someone asks about. But a two-word chunk retrieves
badly, because BM25 length normalisation gives very short documents an advantage.
It also reads as a fragment when cited. Merging keeps the fact and gives it
enough surrounding text to be useful as a citation.
"""

MAX_CHUNK_WORDS = 220
"""A section longer than this is split at paragraph boundaries.

Titan v2 can embed much more than this. The limit is about citations, not
embedding. A citation is only useful if a reader can find the claim inside it. A
chunk the size of a full page is really a document reference, not a citation.
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
        """The text that gets embedded. The heading carries context the body
        assumes.

        A section titled "Lead time" whose body says "Standard production is 7
        business days" is about lead time, whether or not those words appear in
        the paragraph. Embedding the body alone would lose that.
        """
        return f"{self.doc_title} — {self.heading}\n\n{self.text}"


def _slug(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "body"


def _split_long(text: str) -> list[str]:
    """Split a section that is too long at blank lines, never inside a
    sentence."""
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
                # The H1 is the document title. Every chunk already carries it.
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
    """Merge sections that are too short into a neighbouring section.

    The merged chunk keeps the heading of whichever section is substantial. That
    heading is what the citation shows. The short section's heading is written
    inline in the text, so the fact under it keeps its label.
    """
    merged: list[tuple[str, str]] = []
    pending: tuple[str, str] | None = None

    for heading, text in drafts:
        if pending is not None:
            # The substantial section keeps its heading. That heading is what
            # the citation shows. A two-line preamble should not take the id of
            # the section below it. The short text is merged in with its own
            # label, so the fact keeps its context.
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
