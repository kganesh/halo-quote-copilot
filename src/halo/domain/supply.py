"""Supplier and decorator truth: can this actually be made, and by when."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from halo.domain.catalog import DecorationMethod


class Supplier(BaseModel):
    id: str = Field(pattern=r"^sup-[a-z0-9]{6}$")
    name: str
    base_lead_time_days: int = Field(ge=1)
    decoration_methods: list[DecorationMethod] = Field(min_length=1)
    rush_available: bool
    rush_surcharge_pct: Decimal = Field(ge=0, decimal_places=1)


class InventoryRow(BaseModel):
    """On-hand units for one supplier/SKU/colour/size combination."""

    supplier_id: str
    sku: str
    color: str
    size: str
    on_hand: int = Field(ge=0)


class CapacityDay(BaseModel):
    """A decorator's bookable units for one method on one day.

    `booked` is deliberately separate from `capacity` so the supplier MCP server
    answers "can you take 500 more by the 15th", not just "how big are you".
    """

    supplier_id: str
    day: date
    method: DecorationMethod
    capacity_units: int = Field(ge=0)
    booked_units: int = Field(ge=0)

    @property
    def available_units(self) -> int:
        return max(0, self.capacity_units - self.booked_units)
