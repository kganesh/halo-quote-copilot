"""The Atlas knowledge corpus — the RAG source at M3.

Documents carry an effective date because quoting policy changes, and a retrieval
that cites a superseded rule is a wrong answer with a citation attached.
"""

from datetime import date

from pydantic import BaseModel, Field


class AtlasDoc(BaseModel):
    id: str = Field(pattern=r"^atl-[a-z0-9-]+$")
    title: str
    category: str
    effective_date: date
    body: str = Field(min_length=200)

    @property
    def word_count(self) -> int:
        return len(self.body.split())
