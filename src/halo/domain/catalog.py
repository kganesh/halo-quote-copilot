"""Product, pricing and margin truth — what the PIM/OMS MCP server will serve."""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class DecorationMethod(StrEnum):
    SCREEN_PRINT = "screen_print"
    EMBROIDERY = "embroidery"
    HEAT_TRANSFER = "heat_transfer"
    LASER_ENGRAVE = "laser_engrave"
    PAD_PRINT = "pad_print"


class ProductCategory(StrEnum):
    OUTERWEAR = "outerwear"
    KNITS = "knits"
    HEADWEAR = "headwear"
    DRINKWARE = "drinkware"
    BAGS = "bags"
    TECH = "tech"
    WRITING = "writing"


class Product(BaseModel):
    """One orderable SKU."""

    sku: str = Field(pattern=r"^HL-[A-Z]{3}-\d{4}$")
    name: str
    category: ProductCategory
    brand: str
    colors: list[str] = Field(min_length=1)
    sizes: list[str] = Field(min_length=1)
    decoration_methods: list[DecorationMethod] = Field(min_length=1)
    base_cost: Decimal = Field(gt=0, decimal_places=2)
    min_order_qty: int = Field(ge=1)


class PriceTier(BaseModel):
    """Quantity-break pricing. Tiers for one SKU are contiguous and non-overlapping."""

    sku: str
    min_qty: int = Field(ge=1)
    max_qty: int | None = None
    unit_price: Decimal = Field(gt=0, decimal_places=2)


class MarginPolicy(BaseModel):
    """The floor below which a quote needs a human — the M6 approval trigger."""

    category: ProductCategory
    floor_pct: Decimal = Field(gt=0, lt=100, decimal_places=1)
    target_pct: Decimal = Field(gt=0, lt=100, decimal_places=1)
