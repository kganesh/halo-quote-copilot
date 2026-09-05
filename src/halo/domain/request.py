"""What the seller asked for, and what an ungrounded answer looks like.

`UngroundedDraft` is a separate type from `Quote` on purpose. A `Quote` cannot be
created without citations. A draft has none, and never will. Keeping the two
types apart means nothing downstream can mistake M1's output for a real quote.
The type system enforces this, not a comment.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from halo.domain.catalog import DecorationMethod


class QuoteRequest(BaseModel):
    """The seller's sentence, parsed into fields.

    Every field except the product description is optional. Sellers leave things
    out. A missing delivery date is a question to ask the customer, not a value
    to invent.
    """

    account_id: str | None = None
    """The customer this is for. Optional, because a seller often asks about a
    product before naming the account, and required before a quote is assembled:
    a quote with no account is not a quote anyone can send."""
    product_description: str = Field(min_length=1)
    quantity: int | None = Field(default=None, ge=1)
    decoration_method: DecorationMethod | None = None
    imprint_colors: int | None = Field(default=None, ge=1)
    imprint_location: str | None = None
    ship_to_city: str | None = None
    ship_to_state: str | None = None
    needed_by: date | None = None
    budget_usd: Decimal | None = Field(default=None, gt=0)


class UngroundedLine(BaseModel):
    """A quote line with no source. The SKU is invented; no catalogue was read."""

    sku: str
    description: str
    quantity: int = Field(ge=1)
    unit_price: Decimal = Field(gt=0, decimal_places=2)

    @property
    def extended(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))


class UngroundedDraft(BaseModel):
    """M1's complete output: a plausible quote with no sources behind it.

    `assumptions` is required and must not be empty. It makes the invented parts
    visible. The model names each thing it had no way to know. Each of those
    becomes a tool call added in M2 or a retrieval added in M3.
    """

    request: QuoteRequest
    lines: list[UngroundedLine] = Field(min_length=1)
    decoration_setup_fee: Decimal = Field(ge=0, decimal_places=2)
    decoration_run_charge_per_unit: Decimal = Field(ge=0, decimal_places=2)
    shipping_cost: Decimal = Field(ge=0, decimal_places=2)
    promised_ship_date: date
    assumptions: list[str] = Field(min_length=1)

    @property
    def subtotal(self) -> Decimal:
        return sum((line.extended for line in self.lines), Decimal("0.00"))

    @property
    def total(self) -> Decimal:
        units = sum(line.quantity for line in self.lines)
        decoration = self.decoration_setup_fee + self.decoration_run_charge_per_unit * units
        return (self.subtotal + decoration + self.shipping_cost).quantize(Decimal("0.01"))
