"""M0's completion criteria: every generated record validates against its schema.

These tests also fix the invariants that later milestones assume: price tiers
with no gaps, booked capacity that never exceeds total capacity, and seller
account lists that stay inside one tenant.
"""

from decimal import Decimal

import pytest

from conftest import load
from halo.domain.atlas import AtlasDoc
from halo.domain.catalog import MarginPolicy, PriceTier, Product
from halo.domain.org import Account, Seller, Tenant
from halo.domain.supply import CapacityDay, InventoryRow, Supplier

TABLES = [
    ("tenants", Tenant),
    ("accounts", Account),
    ("sellers", Seller),
    ("products", Product),
    ("price_tiers", PriceTier),
    ("margin_policies", MarginPolicy),
    ("suppliers", Supplier),
    ("inventory", InventoryRow),
    ("capacity", CapacityDay),
    ("atlas_docs", AtlasDoc),
]


@pytest.mark.parametrize(("name", "model"), TABLES)
def test_every_record_validates(corpus, name, model):
    rows = load(corpus, name)
    assert rows, f"{name} is empty"
    for row in rows:
        model.model_validate(row)


def test_price_tiers_are_contiguous_and_cover_every_sku(corpus):
    products = {p["sku"] for p in load(corpus, "products")}
    tiers: dict[str, list[dict]] = {}
    for tier in load(corpus, "price_tiers"):
        tiers.setdefault(tier["sku"], []).append(tier)

    assert set(tiers) == products

    for sku, sku_tiers in tiers.items():
        ordered = sorted(sku_tiers, key=lambda t: t["min_qty"])
        assert ordered[0]["min_qty"] == 1, f"{sku} has no tier covering small quantities"
        assert ordered[-1]["max_qty"] is None, f"{sku} has no open-ended top tier"
        for lower, upper in zip(ordered, ordered[1:], strict=False):
            assert lower["max_qty"] + 1 == upper["min_qty"], f"{sku} has a gap or overlap"


def test_unit_price_always_exceeds_cost(corpus):
    costs = {p["sku"]: Decimal(str(p["base_cost"])) for p in load(corpus, "products")}
    for tier in load(corpus, "price_tiers"):
        assert Decimal(str(tier["unit_price"])) > costs[tier["sku"]]


def test_larger_quantities_are_never_more_expensive(corpus):
    tiers: dict[str, list[dict]] = {}
    for tier in load(corpus, "price_tiers"):
        tiers.setdefault(tier["sku"], []).append(tier)
    for sku, sku_tiers in tiers.items():
        ordered = sorted(sku_tiers, key=lambda t: t["min_qty"])
        prices = [Decimal(str(t["unit_price"])) for t in ordered]
        assert prices == sorted(prices, reverse=True), f"{sku} price rises with quantity"


def test_a_seller_book_stays_inside_its_tenant(corpus):
    tenant_of = {a["id"]: a["tenant_id"] for a in load(corpus, "accounts")}
    for seller in load(corpus, "sellers"):
        for account_id in seller["account_ids"]:
            assert tenant_of[account_id] == seller["tenant_id"]


def test_some_seller_pairs_have_no_overlap(corpus):
    """M5's deny test needs a request that is clearly out of scope."""
    sellers = [s for s in load(corpus, "sellers") if s["role"] == "seller"]
    books = [set(s["account_ids"]) for s in sellers]
    assert any(not a & b for a in books for b in books if a is not b)


def test_capacity_is_never_overbooked(corpus):
    for day in load(corpus, "capacity"):
        assert day["booked_units"] <= day["capacity_units"]


def test_capacity_includes_both_full_and_open_days(corpus):
    """If no day is ever full, confirming a date is a formality."""
    days = load(corpus, "capacity")
    assert any(d["booked_units"] == d["capacity_units"] for d in days)
    assert any(d["booked_units"] < d["capacity_units"] for d in days)


def test_inventory_includes_stockouts(corpus):
    rows = load(corpus, "inventory")
    assert any(r["on_hand"] == 0 for r in rows)
    assert any(r["on_hand"] > 0 for r in rows)


def test_suppliers_only_stock_methods_they_offer(corpus):
    offered = {s["id"]: set(s["decoration_methods"]) for s in load(corpus, "suppliers")}
    for day in load(corpus, "capacity"):
        assert day["method"] in offered[day["supplier_id"]]


def test_atlas_docs_are_substantial_and_unique(corpus):
    docs = load(corpus, "atlas_docs")
    assert len({d["id"] for d in docs}) == len(docs)
    assert len({d["body"] for d in docs}) == len(docs), "duplicate bodies would skew retrieval"
    for doc in docs:
        assert len(doc["body"].split()) >= 60, f"{doc['id']} is too thin to chunk"


def test_atlas_markdown_files_match_the_records(corpus):
    docs = load(corpus, "atlas_docs")
    files = sorted((corpus / "atlas").glob("*.md"))
    assert len(files) == len(docs)
    for doc in docs:
        assert (corpus / "atlas" / f"{doc['id']}.md").read_text().strip() == doc["body"].strip()
