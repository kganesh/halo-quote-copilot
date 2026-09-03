"""The Atlas knowledge corpus. This is the RAG source used in M3.

Documents carry an effective date because quoting policy changes over time. A
retrieval that cites a superseded rule produces a wrong answer that still has a
citation attached.
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
