"""PIM / OMS: what HALO sells, at what price, at what margin floor.

The system of record for money. Every unit price in a quote has to come from
`get_price`, and the `tool_call_id` of that call is what the quote cites.
"""

from __future__ import annotations

from decimal import Decimal

from mcp.server.mcpserver import MCPServer

from halo.mcp_servers.store import margin_policies, money, price_tiers, products

server = MCPServer(name="halo-pim-oms", description="HALO product, pricing and margin truth")


@server.tool()
def search_products(description: str, category: str | None = None, limit: int = 5) -> list[dict]:
    """Find catalogue SKUs matching a plain-English product description.

    Args:
        description: what the customer asked for, e.g. "mid-weight fleece hoodie".
        category: optional catalogue category to narrow to.
        limit: how many matches to return.
    """
    terms = {word for word in description.lower().split() if len(word) > 3}
    scored: list[tuple[int, dict]] = []
    for product in products():
        if category and product["category"] != category:
            continue
        haystack = f"{product['name']} {product['category']}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, product))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["sku"]))
    return [
        {
            "sku": p["sku"],
            "name": p["name"],
            "category": p["category"],
            "colors": p["colors"],
            "sizes": p["sizes"],
            "decoration_methods": p["decoration_methods"],
            "min_order_qty": p["min_order_qty"],
        }
        for _, p in scored[:limit]
    ]


@server.tool()
def get_price(sku: str, quantity: int) -> dict:
    """Unit price for a SKU at a quantity, from the quantity-break table.

    Returns the tier that applies, so a caller can see which break it landed in
    rather than only the number that came out.
    """
    tiers = [t for t in price_tiers() if t["sku"] == sku]
    if not tiers:
        return {"error": f"unknown sku {sku}"}

    applicable = next(
        (
            t
            for t in tiers
            if t["min_qty"] <= quantity and (t["max_qty"] is None or quantity <= t["max_qty"])
        ),
        None,
    )
    if applicable is None:
        return {"error": f"no price tier covers quantity {quantity} for {sku}"}

    product = next(p for p in products() if p["sku"] == sku)
    unit_price = money(applicable["unit_price"])
    base_cost = money(product["base_cost"])
    return {
        "sku": sku,
        "quantity": quantity,
        "unit_price": str(unit_price),
        "extended": str((unit_price * quantity).quantize(Decimal("0.01"))),
        "base_cost": str(base_cost),
        "tier": f"{applicable['min_qty']}-{applicable['max_qty'] or 'plus'}",
        "min_order_qty": product["min_order_qty"],
    }


@server.tool()
def get_margin_policy(category: str) -> dict:
    """The floor and target gross margin for a category.

    The floor is what triggers human approval, so it is served rather than
    remembered.
    """
    policy = next((p for p in margin_policies() if p["category"] == category), None)
    if policy is None:
        return {"error": f"no margin policy for category {category}"}
    return {
        "category": category,
        "floor_pct": str(money(policy["floor_pct"])),
        "target_pct": str(money(policy["target_pct"])),
    }


if __name__ == "__main__":
    server.run(transport="stdio")
