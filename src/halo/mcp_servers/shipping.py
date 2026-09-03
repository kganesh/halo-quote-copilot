"""Carrier: transit time and freight cost to the destination.

Transit is measured from the decorating supplier's hub, not from HALO's office.
The freight policy makes this distinction. It is the distinction that gets lost
when a promised delivery date turns out to be a promised ship date.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="halo-shipping", description="Ground transit estimates and freight")

# Synthetic zone table, keyed by destination state, measured from a Midwest hub.
# The transit days match those stated in atl-freight-and-zones.
ZONE_BY_STATE = {
    "IL": 2,
    "WI": 2,
    "IN": 2,
    "IA": 2,
    "MO": 3,
    "OH": 3,
    "MI": 3,
    "MN": 3,
    "KY": 3,
    "PA": 4,
    "NY": 4,
    "TN": 4,
    "KS": 4,
    "NE": 4,
    "MA": 5,
    "CT": 5,
    "NJ": 5,
    "GA": 5,
    "NC": 5,
    "TX": 5,
    "CO": 5,
    "FL": 6,
    "AZ": 6,
    "CA": 6,
    "OR": 6,
    "WA": 6,
    "NV": 6,
}
TRANSIT_DAYS_BY_ZONE = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
RATE_PER_POUND_BY_ZONE = {
    2: Decimal("0.42"),
    3: Decimal("0.55"),
    4: Decimal("0.68"),
    5: Decimal("0.81"),
    6: Decimal("0.97"),
}
RESIDENTIAL_SURCHARGE = Decimal("6.50")
SPLIT_ADDRESS_SURCHARGE = Decimal("12.00")


@server.tool()
def estimate_freight(
    to_state: str,
    units: int,
    pounds_per_unit: float = 1.4,
    residential: bool = False,
    extra_addresses: int = 0,
) -> dict:
    """Ground transit days and freight cost to a destination state.

    Args:
        to_state: two-letter destination state code.
        units: pieces shipping.
        pounds_per_unit: shipping weight per piece; apparel runs about 1.4 lb.
        residential: whether delivery is to a residential address.
        extra_addresses: additional delivery addresses beyond the first.
    """
    state = to_state.strip().upper()
    zone = ZONE_BY_STATE.get(state)
    if zone is None:
        return {"error": f"no zone on file for state {state!r}"}

    weight = Decimal(str(pounds_per_unit)) * units
    cost = RATE_PER_POUND_BY_ZONE[zone] * weight
    if residential:
        cost += RESIDENTIAL_SURCHARGE
    cost += SPLIT_ADDRESS_SURCHARGE * extra_addresses

    return {
        "to_state": state,
        "zone": zone,
        "transit_days": TRANSIT_DAYS_BY_ZONE[zone],
        "billable_pounds": str(weight.quantize(Decimal("0.1"))),
        "freight_cost": str(cost.quantize(Decimal("0.01"))),
    }


@server.tool()
def delivery_date(ship_date: str, to_state: str) -> dict:
    """When a shipment leaving on `ship_date` arrives, counted in business days.

    This function exists to prevent one specific error: quoting a delivery date
    without adding transit time to the ship date.
    """
    state = to_state.strip().upper()
    zone = ZONE_BY_STATE.get(state)
    if zone is None:
        return {"error": f"no zone on file for state {state!r}"}
    try:
        shipped = date.fromisoformat(ship_date)
    except ValueError as exc:
        return {"error": f"bad date: {exc}"}

    remaining = TRANSIT_DAYS_BY_ZONE[zone]
    arrives = shipped
    while remaining:
        arrives += timedelta(days=1)
        if arrives.weekday() < 5:
            remaining -= 1

    return {
        "ship_date": ship_date,
        "to_state": state,
        "zone": zone,
        "transit_days": TRANSIT_DAYS_BY_ZONE[zone],
        "delivery_date": arrives.isoformat(),
    }


if __name__ == "__main__":
    server.run(transport="stdio")
