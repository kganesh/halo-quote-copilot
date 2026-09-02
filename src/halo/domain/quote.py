"""The quote itself, and the provenance rule that makes it trustworthy.

Design rule 02: every number a seller might act on carries a citation. The
assembler refuses to emit a quote with an uncited figure, which is what turns a
hallucinated price from something you notice in a demo into a failing test.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, model_validator

from halo.domain.catalog import DecorationMethod


class CitationKind(StrEnum):
    """Where a claim came from. There is no third option — that is the point."""

    CHUNK = "chunk"
    TOOL_CALL = "tool_call"


# `min_length=1` alone accepts a single space, which is exactly the shape a
# fabricated citation takes: present, non-empty, and carrying nothing.
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Citation(BaseModel):
    """A pointer back to the evidence, plus the span that actually supports it.

    `supporting_text` is stored rather than re-fetched so a stored quote stays
    auditable after the underlying document is revised.
    """

    kind: CitationKind
    ref: NonBlank
    supporting_text: NonBlank


class QuoteLine(BaseModel):
    sku: str
    description: str
    quantity: int = Field(ge=1)
    unit_price: Decimal = Field(gt=0, decimal_places=2)
    citations: list[Citation] = Field(min_length=1)

    @property
    def extended(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))


class DecorationCharge(BaseModel):
    method: DecorationMethod
    colors: int = Field(ge=1)
    setup_fee: Decimal = Field(ge=0, decimal_places=2)
    run_charge_per_unit: Decimal = Field(ge=0, decimal_places=2)
    citations: list[Citation] = Field(min_length=1)


class Quote(BaseModel):
    """A draft quote. Every monetary field traces to a citation somewhere."""

    request_id: str
    account_id: str
    lines: list[QuoteLine] = Field(min_length=1)
    decoration: DecorationCharge
    shipping_cost: Decimal = Field(ge=0, decimal_places=2)
    shipping_citations: list[Citation] = Field(min_length=1)
    promised_ship_date: date
    margin_pct: Decimal = Field(decimal_places=1)

    @property
    def all_citations(self) -> list[Citation]:
        """Every citation the quote rests on, for the M7 evidence envelope."""
        found = list(self.decoration.citations) + list(self.shipping_citations)
        for line in self.lines:
            found.extend(line.citations)
        return found

    @property
    def subtotal(self) -> Decimal:
        return sum((line.extended for line in self.lines), Decimal("0.00"))

    @property
    def total(self) -> Decimal:
        units = sum(line.quantity for line in self.lines)
        decoration_total = self.decoration.setup_fee + self.decoration.run_charge_per_unit * units
        return (self.subtotal + decoration_total + self.shipping_cost).quantize(Decimal("0.01"))

    @model_validator(mode="after")
    def _citation_refs_match_their_kind(self) -> "Quote":
        """Rule 02, enforced in the type rather than in a prompt.

        The field constraints already guarantee every figure carries a non-blank
        citation. What they cannot see is a ref pointing at the wrong kind of
        thing — a document id filed as a tool call, which reads fine in a quote
        and breaks the audit trail at M7 when nothing resolves it.
        """
        for citation in self.all_citations:
            expected = "atl-" if citation.kind is CitationKind.CHUNK else "tc-"
            if not citation.ref.startswith(expected):
                raise ValueError(
                    f"{citation.kind} citation ref {citation.ref!r} should start with {expected!r}"
                )
        return self
