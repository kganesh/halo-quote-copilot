"""Regenerate the whole synthetic HALO corpus from one fixed seed.

Determinism is the point. Two runs on two machines produce identical files. A
retrieval or pricing regression at M3 is therefore a real regression, not a
reshuffled catalogue. Everything here is invented. No HALO systems or customer
records are involved.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from halo.domain.atlas import AtlasDoc
from halo.domain.catalog import DecorationMethod, MarginPolicy, PriceTier, Product, ProductCategory
from halo.domain.org import Account, Seller, Tenant
from halo.domain.supply import CapacityDay, InventoryRow, Supplier
from halo.seed import SEED
from halo.seed.atlas_sources import (
    CARE_SHEET_TEMPLATE,
    CATEGORY_NOTES,
    CORE_DOCS,
    SUPPLIER_ADDENDUM_TEMPLATE,
)

OUT_DIR = Path(__file__).resolve().parents[3] / "data" / "seed"

# The catalogue is anchored to a fixed "today". Otherwise capacity calendars and
# effective dates would move with the real clock and change eval answers without
# any code change.
ANCHOR_DAY = date(2026, 9, 1)
CAPACITY_HORIZON_DAYS = 90

CATEGORY_CODES = {
    ProductCategory.OUTERWEAR: "OUT",
    ProductCategory.KNITS: "KNT",
    ProductCategory.HEADWEAR: "HDW",
    ProductCategory.DRINKWARE: "DRK",
    ProductCategory.BAGS: "BAG",
    ProductCategory.TECH: "TCH",
    ProductCategory.WRITING: "WRT",
}

CATEGORY_METHODS: dict[ProductCategory, list[DecorationMethod]] = {
    ProductCategory.OUTERWEAR: [
        DecorationMethod.SCREEN_PRINT,
        DecorationMethod.EMBROIDERY,
        DecorationMethod.HEAT_TRANSFER,
    ],
    ProductCategory.KNITS: [
        DecorationMethod.SCREEN_PRINT,
        DecorationMethod.EMBROIDERY,
        DecorationMethod.HEAT_TRANSFER,
    ],
    ProductCategory.HEADWEAR: [DecorationMethod.EMBROIDERY, DecorationMethod.HEAT_TRANSFER],
    ProductCategory.DRINKWARE: [DecorationMethod.LASER_ENGRAVE, DecorationMethod.PAD_PRINT],
    ProductCategory.BAGS: [DecorationMethod.SCREEN_PRINT, DecorationMethod.EMBROIDERY],
    ProductCategory.TECH: [DecorationMethod.LASER_ENGRAVE, DecorationMethod.PAD_PRINT],
    ProductCategory.WRITING: [DecorationMethod.PAD_PRINT, DecorationMethod.LASER_ENGRAVE],
}

CATEGORY_COST_RANGE: dict[ProductCategory, tuple[float, float]] = {
    ProductCategory.OUTERWEAR: (18.00, 62.00),
    ProductCategory.KNITS: (4.50, 22.00),
    ProductCategory.HEADWEAR: (5.00, 19.00),
    ProductCategory.DRINKWARE: (6.00, 28.00),
    ProductCategory.BAGS: (7.50, 34.00),
    ProductCategory.TECH: (9.00, 48.00),
    ProductCategory.WRITING: (0.60, 6.50),
}

CATEGORY_SIZES: dict[ProductCategory, list[str]] = {
    ProductCategory.OUTERWEAR: ["S", "M", "L", "XL", "2XL"],
    ProductCategory.KNITS: ["S", "M", "L", "XL", "2XL", "3XL"],
    ProductCategory.HEADWEAR: ["OSFA"],
    ProductCategory.DRINKWARE: ["12oz", "20oz", "32oz"],
    ProductCategory.BAGS: ["OS"],
    ProductCategory.TECH: ["OS"],
    ProductCategory.WRITING: ["OS"],
}

# These floors and targets must match atl-margin-floors. If they do not,
# retrieval will cite a document that contradicts the tool. Defining them in one
# place prevents that.
MARGIN_FLOORS: dict[ProductCategory, tuple[str, str]] = {
    ProductCategory.OUTERWEAR: ("32.0", "38.0"),
    ProductCategory.KNITS: ("30.0", "36.0"),
    ProductCategory.HEADWEAR: ("33.0", "40.0"),
    ProductCategory.DRINKWARE: ("35.0", "42.0"),
    ProductCategory.BAGS: ("31.0", "37.0"),
    ProductCategory.TECH: ("28.0", "34.0"),
    ProductCategory.WRITING: ("36.0", "44.0"),
}

COLORS = [
    "Black",
    "White",
    "Navy",
    "Heather Grey",
    "Forest",
    "Maroon",
    "Royal",
    "Charcoal",
    "Sand",
    "Cardinal",
]

PRODUCT_NOUNS: dict[ProductCategory, list[str]] = {
    ProductCategory.OUTERWEAR: [
        "Softshell Jacket",
        "Quarter Zip",
        "Puffer Vest",
        "Hooded Sweatshirt",
    ],
    ProductCategory.KNITS: [
        "Ring-Spun Tee",
        "Performance Polo",
        "Long Sleeve Tee",
        "Fleece Hoodie",
    ],
    ProductCategory.HEADWEAR: ["Structured Cap", "Trucker Cap", "Beanie", "Bucket Hat"],
    ProductCategory.DRINKWARE: ["Vacuum Tumbler", "Water Bottle", "Ceramic Mug", "Can Cooler"],
    ProductCategory.BAGS: ["Backpack", "Tote", "Duffel", "Laptop Sleeve"],
    ProductCategory.TECH: ["Wireless Charger", "Bluetooth Speaker", "Power Bank", "Earbud Case"],
    ProductCategory.WRITING: ["Gel Pen", "Metal Rollerball", "Stylus Pen", "Highlighter Set"],
}

BRANDS = ["Meridian", "Northgate", "Fairhaven", "Copperline", "Halcyon Supply"]

TENANTS = [
    ("tnt-mwest1", "HALO Midwest", "Midwest"),
    ("tnt-neast1", "HALO Northeast", "Northeast"),
    ("tnt-wcoast", "HALO West", "West"),
]

SUPPLIERS = [
    (
        "sup-apex01",
        "Apex Decorating",
        7,
        [DecorationMethod.SCREEN_PRINT, DecorationMethod.HEAT_TRANSFER],
        True,
        "30.0",
    ),
    ("sup-stitch", "Stitchcraft Embroidery", 10, [DecorationMethod.EMBROIDERY], True, "35.0"),
    (
        "sup-lakes1",
        "Great Lakes Imprint",
        8,
        [DecorationMethod.SCREEN_PRINT, DecorationMethod.EMBROIDERY],
        False,
        "0.0",
    ),
    (
        "sup-etch01",
        "Etchworks",
        6,
        [DecorationMethod.LASER_ENGRAVE, DecorationMethod.PAD_PRINT],
        True,
        "25.0",
    ),
    ("sup-rapid1", "RapidTransfer Co", 5, [DecorationMethod.HEAT_TRANSFER], True, "40.0"),
    (
        "sup-summit",
        "Summit Hard Goods",
        9,
        [DecorationMethod.PAD_PRINT, DecorationMethod.LASER_ENGRAVE],
        False,
        "0.0",
    ),
]

CITIES = [
    ("Chicago", "IL"),
    ("Milwaukee", "WI"),
    ("Columbus", "OH"),
    ("Boston", "MA"),
    ("Hartford", "CT"),
    ("Philadelphia", "PA"),
    ("Denver", "CO"),
    ("Portland", "OR"),
    ("San Jose", "CA"),
]

ACCOUNT_PREFIXES = ["Cedar", "Brightline", "Kestrel", "Ironwood", "Vantage", "Larkspur"]
ACCOUNT_SUFFIXES = ["Health", "Partners", "Group", "Systems", "Industries", "College"]

INDUSTRIES = [
    "healthcare",
    "financial services",
    "higher education",
    "construction",
    "software",
    "logistics",
    "hospitality",
    "manufacturing",
]


def _money(rng: random.Random, low: float, high: float) -> Decimal:
    return Decimal(f"{rng.uniform(low, high):.2f}")


def build_tenants() -> list[Tenant]:
    return [Tenant(id=i, name=n, region=r) for i, n, r in TENANTS]


def build_accounts(rng: random.Random) -> list[Account]:
    accounts: list[Account] = []
    for tenant_id, _, _ in TENANTS:
        for n in range(8):
            city, state = CITIES[rng.randrange(len(CITIES))]
            accounts.append(
                Account(
                    id=f"acct-{tenant_id[4:8]}{n:02d}",
                    tenant_id=tenant_id,
                    name=f"{rng.choice(ACCOUNT_PREFIXES)} {rng.choice(ACCOUNT_SUFFIXES)}",
                    industry=rng.choice(INDUSTRIES),
                    ship_to_city=city,
                    ship_to_state=state,
                )
            )
    return accounts


def build_sellers(rng: random.Random, accounts: list[Account]) -> list[Seller]:
    """Twelve sellers, four per tenant. One per tenant is a manager.

    Seller account lists deliberately do not overlap. The M5 deny test needs a
    request that is clearly outside a seller's scope.
    """
    first = [
        "Dana",
        "Priya",
        "Marcus",
        "Elena",
        "Tomas",
        "Aisha",
        "Ravi",
        "Nora",
        "Ilya",
        "Grace",
        "Owen",
        "Mei",
    ]
    last = [
        "Whitfield",
        "Nair",
        "Okafor",
        "Brandt",
        "Reyes",
        "Haddad",
        "Menon",
        "Lindqvist",
        "Petrov",
        "Adeyemi",
        "Gallagher",
        "Chen",
    ]
    sellers: list[Seller] = []
    idx = 0
    for tenant_id, _, _ in TENANTS:
        book = [a.id for a in accounts if a.tenant_id == tenant_id]
        for seat in range(4):
            name = f"{first[idx]} {last[idx]}"
            assigned = book[seat * 2 : seat * 2 + 2]
            sellers.append(
                Seller(
                    id=f"usr-{tenant_id[4:8]}{seat:02d}",
                    tenant_id=tenant_id,
                    name=name,
                    email=f"{first[idx].lower()}.{last[idx].lower()}@example-halo.test",
                    role="sales_manager" if seat == 0 else "seller",
                    account_ids=book if seat == 0 else assigned,
                )
            )
            idx += 1
    return sellers


def build_catalog(rng: random.Random) -> tuple[list[Product], list[PriceTier]]:
    products: list[Product] = []
    tiers: list[PriceTier] = []
    per_category = 200 // len(ProductCategory)

    for category in ProductCategory:
        low, high = CATEGORY_COST_RANGE[category]
        for n in range(per_category):
            sku = f"HL-{CATEGORY_CODES[category]}-{1000 + n:04d}"
            cost = _money(rng, low, high)
            products.append(
                Product(
                    sku=sku,
                    name=f"{rng.choice(BRANDS)} {rng.choice(PRODUCT_NOUNS[category])}",
                    category=category,
                    brand=rng.choice(BRANDS),
                    colors=rng.sample(COLORS, k=rng.randint(3, 6)),
                    sizes=CATEGORY_SIZES[category],
                    decoration_methods=CATEGORY_METHODS[category],
                    base_cost=cost,
                    min_order_qty=24
                    if category in (ProductCategory.KNITS, ProductCategory.OUTERWEAR)
                    else 48,
                )
            )
            # Four quantity breaks. Each has a lower markup than the one before.
            for (lo, hi), markup in zip(
                [(1, 99), (100, 249), (250, 499), (500, None)],
                [Decimal("2.05"), Decimal("1.85"), Decimal("1.70"), Decimal("1.58")],
                strict=True,
            ):
                tiers.append(
                    PriceTier(
                        sku=sku,
                        min_qty=lo,
                        max_qty=hi,
                        unit_price=(cost * markup).quantize(Decimal("0.01")),
                    )
                )
    return products, tiers


def build_margin_policies() -> list[MarginPolicy]:
    return [
        MarginPolicy(category=c, floor_pct=Decimal(f), target_pct=Decimal(t))
        for c, (f, t) in MARGIN_FLOORS.items()
    ]


def build_suppliers() -> list[Supplier]:
    return [
        Supplier(
            id=sid,
            name=name,
            base_lead_time_days=lead,
            decoration_methods=methods,
            rush_available=rush,
            rush_surcharge_pct=Decimal(pct),
        )
        for sid, name, lead, methods, rush, pct in SUPPLIERS
    ]


def build_inventory(
    rng: random.Random, products: list[Product], suppliers: list[Supplier]
) -> list[InventoryRow]:
    """Each supplier stocks part of the catalogue, including some stockouts.

    About one row in twelve has zero on hand. That makes "is it available" a
    question with two possible answers instead of a formality.
    """
    rows: list[InventoryRow] = []
    for supplier in suppliers:
        stocked = rng.sample(products, k=40)
        for product in stocked:
            for color in product.colors[:3]:
                for size in product.sizes[:3]:
                    on_hand = 0 if rng.random() < 0.08 else rng.randrange(80, 4000)
                    rows.append(
                        InventoryRow(
                            supplier_id=supplier.id,
                            sku=product.sku,
                            color=color,
                            size=size,
                            on_hand=on_hand,
                        )
                    )
    return rows


def build_capacity(rng: random.Random, suppliers: list[Supplier]) -> list[CapacityDay]:
    """Ninety days of bookable capacity, weekdays only, including full days."""
    days: list[CapacityDay] = []
    for supplier in suppliers:
        for offset in range(CAPACITY_HORIZON_DAYS):
            day = ANCHOR_DAY + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            for method in supplier.decoration_methods:
                capacity = rng.randrange(600, 2400, 100)
                # About a fifth of days are fully booked. Those are the days
                # that make "confirm the date" different from "read the lead
                # time".
                booked = capacity if rng.random() < 0.2 else rng.randrange(0, capacity)
                days.append(
                    CapacityDay(
                        supplier_id=supplier.id,
                        day=day,
                        method=method,
                        capacity_units=capacity,
                        booked_units=booked,
                    )
                )
    return days


def build_atlas(suppliers: list[Supplier]) -> list[AtlasDoc]:
    docs = [
        AtlasDoc(id=i, title=t, category=c, effective_date=d, body=b) for i, t, c, d, b in CORE_DOCS
    ]

    for category in ProductCategory:
        decoration_note, care_note = CATEGORY_NOTES[category.value]
        title = category.value.replace("_", " ").title()
        docs.append(
            AtlasDoc(
                id=f"atl-care-{category.value}",
                title=f"{title} Specification and Care",
                category="product",
                effective_date=date(2026, 3, 1),
                body=CARE_SHEET_TEMPLATE.format(
                    category_title=title,
                    category_title_lower=title.lower(),
                    decoration_note=decoration_note,
                    care_note=care_note,
                ),
            )
        )

    for supplier in suppliers:
        methods = "\n".join(
            f"- {m.value.replace('_', ' ').title()}" for m in supplier.decoration_methods
        )
        rush_note = (
            f"Available at a {supplier.rush_surcharge_pct}% surcharge on decoration, "
            "subject to a same-day capacity confirmation."
            if supplier.rush_available
            else "Not offered. A date this supplier cannot meet at standard lead time "
            "needs a different supplier, not a surcharge."
        )
        docs.append(
            AtlasDoc(
                id=f"atl-supplier-{supplier.id[4:]}",
                title=f"{supplier.name} Decoration Addendum",
                category="supplier",
                effective_date=date(2026, 4, 15),
                body=SUPPLIER_ADDENDUM_TEMPLATE.format(
                    supplier_name=supplier.name,
                    methods=methods,
                    lead_time=supplier.base_lead_time_days,
                    rush_note=rush_note,
                ),
            )
        )
    return docs


def _write(name: str, records: list[BaseModel]) -> Path:
    path = OUT_DIR / f"{name}.json"
    payload = [r.model_dump(mode="json") for r in records]
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def generate(out_dir: Path = OUT_DIR) -> dict[str, int]:
    """Write the corpus and return a name -> record count summary."""
    global OUT_DIR
    OUT_DIR = out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "atlas").mkdir(exist_ok=True)

    rng = random.Random(SEED)

    tenants = build_tenants()
    accounts = build_accounts(rng)
    sellers = build_sellers(rng, accounts)
    products, tiers = build_catalog(rng)
    margins = build_margin_policies()
    suppliers = build_suppliers()
    inventory = build_inventory(rng, products, suppliers)
    capacity = build_capacity(rng, suppliers)
    atlas = build_atlas(suppliers)

    tables = {
        "tenants": tenants,
        "accounts": accounts,
        "sellers": sellers,
        "products": products,
        "price_tiers": tiers,
        "margin_policies": margins,
        "suppliers": suppliers,
        "inventory": inventory,
        "capacity": capacity,
        "atlas_docs": atlas,
    }
    for name, records in tables.items():
        _write(name, records)

    # Atlas is also written as markdown files. The M3 ingest reads files rather
    # than a single JSON document, because chunking a real document is part of
    # what M3 is for.
    for doc in atlas:
        (out_dir / "atlas" / f"{doc.id}.md").write_text(doc.body + "\n")

    return {name: len(records) for name, records in tables.items()}


def main() -> None:
    summary = generate()
    width = max(len(k) for k in summary)
    print(f"seed {SEED} -> {OUT_DIR}")
    for name, count in summary.items():
        print(f"  {name.ljust(width)}  {count:>6,}")


if __name__ == "__main__":
    main()
