"""Supplier / decorator: can this be made, by whom, by when, at what decoration cost.

Capacity is the point of this server. A lead time is an average; a date is a
commitment, and only a day-by-day capacity check can support one.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from mcp.server.mcpserver import MCPServer

from halo.mcp_servers.store import capacity, inventory, suppliers

server = MCPServer(
    name="halo-supplier", description="Supplier stock, decoration capacity and charges"
)

# Kept in step with atl-screen-print-standards and atl-embroidery-standards. A
# test asserts they still agree — a tool and a policy document that quietly
# disagree is the failure a grounding check cannot see.
SETUP_PER_COLOR = {"screen_print": Decimal("22.00"), "pad_print": Decimal("28.00")}
SETUP_FLAT = {"laser_engrave": Decimal("40.00"), "embroidery": Decimal("65.00")}
RUN_FIRST_COLOR = {
    "screen_print": Decimal("0.85"),
    "pad_print": Decimal("0.60"),
    "laser_engrave": Decimal("1.10"),
    "heat_transfer": Decimal("2.10"),
}
RUN_EXTRA_COLOR = {"screen_print": Decimal("0.35"), "pad_print": Decimal("0.60")}
HEAT_TRANSFER_BREAK_QTY = 144
HEAT_TRANSFER_HIGH_QTY_RUN = Decimal("1.65")


@server.tool()
def check_inventory(
    sku: str, quantity: int, color: str | None = None, method: str | None = None
) -> list[dict]:
    """Which suppliers hold enough of a SKU to cover a quantity.

    Sums across sizes, because a 500-piece apparel order is a size run rather
    than 500 of one size.

    Args:
        sku: the catalogue SKU.
        quantity: pieces needed.
        color: optional colour to restrict to.
        method: optional decoration method — pass it and only suppliers who can
            actually decorate the goods come back. Without it the answer includes
            warehouses that cannot do the job, which is a trap rather than a fact.
    """
    offering = {s["id"] for s in suppliers() if method is None or method in s["decoration_methods"]}
    rows = [
        r
        for r in inventory()
        if r["sku"] == sku
        and (color is None or r["color"] == color)
        and r["supplier_id"] in offering
    ]
    by_supplier: dict[str, int] = {}
    for row in rows:
        by_supplier[row["supplier_id"]] = by_supplier.get(row["supplier_id"], 0) + row["on_hand"]

    # Decoration methods travel with the stock answer on purpose. Holding the
    # goods and being able to decorate them are different capabilities, and a
    # caller given only the first will pick a supplier that cannot do the job —
    # which is exactly what happened before this field existed.
    by_id = {s["id"]: s for s in suppliers()}
    return sorted(
        (
            {
                "supplier_id": supplier_id,
                "supplier_name": by_id.get(supplier_id, {}).get("name", supplier_id),
                "on_hand": on_hand,
                "sufficient": on_hand >= quantity,
                "decoration_methods": by_id.get(supplier_id, {}).get("decoration_methods", []),
            }
            for supplier_id, on_hand in by_supplier.items()
        ),
        key=lambda r: (-r["on_hand"], r["supplier_id"]),
    )


@server.tool()
def find_capacity(method: str, units: int, not_before: str, not_after: str) -> list[dict]:
    """Suppliers with enough free decorating capacity on a day in the window.

    Args:
        method: decoration method, e.g. "screen_print".
        units: pieces needing decoration.
        not_before: earliest acceptable production day, ISO date.
        not_after: latest acceptable production day, ISO date.
    """
    try:
        start, end = date.fromisoformat(not_before), date.fromisoformat(not_after)
    except ValueError as exc:
        return [{"error": f"bad date: {exc}"}]

    offering = {s["id"]: s for s in suppliers() if method in s["decoration_methods"]}
    found: list[dict] = []
    for day in capacity():
        if day["supplier_id"] not in offering or day["method"] != method:
            continue
        when = date.fromisoformat(day["day"])
        if not (start <= when <= end):
            continue
        available = day["capacity_units"] - day["booked_units"]
        if available >= units:
            supplier = offering[day["supplier_id"]]
            found.append(
                {
                    "supplier_id": supplier["id"],
                    "supplier_name": supplier["name"],
                    "day": day["day"],
                    "available_units": available,
                    "base_lead_time_days": supplier["base_lead_time_days"],
                    "rush_available": supplier["rush_available"],
                }
            )

    found.sort(key=lambda r: (r["day"], r["supplier_id"]))
    return found[:10]


@server.tool()
def get_decoration_charges(method: str, colors: int, units: int) -> dict:
    """Setup and per-unit run charges for a decoration job.

    Colour count drives screen and pad print; embroidery and laser are flat,
    because they are priced by stitches and by design respectively.
    """
    if method not in RUN_FIRST_COLOR and method != "embroidery":
        return {"error": f"unknown decoration method {method}"}

    setup = SETUP_FLAT.get(method, SETUP_PER_COLOR.get(method, Decimal("0.00")) * colors)

    if method == "heat_transfer":
        run = (
            HEAT_TRANSFER_HIGH_QTY_RUN
            if units >= HEAT_TRANSFER_BREAK_QTY
            else RUN_FIRST_COLOR[method]
        )
    elif method == "embroidery":
        run = Decimal("0.00")
    else:
        extra = RUN_EXTRA_COLOR.get(method, Decimal("0.00")) * max(0, colors - 1)
        run = RUN_FIRST_COLOR[method] + extra

    return {
        "method": method,
        "colors": colors,
        "units": units,
        "setup_fee": str(setup.quantize(Decimal("0.01"))),
        "run_charge_per_unit": str(run.quantize(Decimal("0.01"))),
        "decoration_total": str((setup + run * units).quantize(Decimal("0.01"))),
    }


@server.tool()
def earliest_ship_date(supplier_id: str, method: str, units: int, from_day: str) -> dict:
    """First day this supplier can finish the run, capacity and lead time both.

    Lead time alone answers "how long do you usually take"; this answers "when
    will mine be done", which is the question a promised date rests on.
    """
    supplier = next((s for s in suppliers() if s["id"] == supplier_id), None)
    if supplier is None:
        return {"error": f"unknown supplier {supplier_id}"}
    if method not in supplier["decoration_methods"]:
        # Naming who does offer it turns a dead end into a next step. An agent
        # told only "no" will usually try another wrong supplier.
        alternatives = [s["name"] for s in suppliers() if method in s["decoration_methods"]]
        return {
            "error": f"{supplier['name']} does not offer {method}. "
            f"Suppliers that do: {', '.join(alternatives) or 'none'}"
        }

    try:
        start = date.fromisoformat(from_day)
    except ValueError as exc:
        return {"error": f"bad date: {exc}"}

    ready = start + timedelta(days=supplier["base_lead_time_days"])
    for day in sorted(capacity(), key=lambda d: d["day"]):
        if day["supplier_id"] != supplier_id or day["method"] != method:
            continue
        when = date.fromisoformat(day["day"])
        if when < ready:
            continue
        if day["capacity_units"] - day["booked_units"] >= units:
            return {
                "supplier_id": supplier_id,
                "supplier_name": supplier["name"],
                "method": method,
                "units": units,
                "ship_date": day["day"],
                "lead_time_days": supplier["base_lead_time_days"],
            }
    return {"error": f"no capacity for {units} units of {method} at {supplier['name']}"}


if __name__ == "__main__":
    server.run(transport="stdio")
