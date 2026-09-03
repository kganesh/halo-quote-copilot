"""The MCP servers, called as functions. Protocol wiring has its own test."""

from decimal import Decimal

from halo.mcp_servers import pim_oms, shipping, supplier


class TestPimOms:
    def test_search_finds_something_for_a_plain_description(self):
        found = pim_oms.search_products("mid-weight fleece hoodie", category="knits")
        assert found
        assert all(p["category"] == "knits" for p in found)

    def test_price_reports_the_tier_it_landed_in(self):
        sku = pim_oms.search_products("hoodie", category="knits")[0]["sku"]
        priced = pim_oms.get_price(sku, 500)
        assert priced["tier"] == "500-plus"
        assert Decimal(priced["unit_price"]) > Decimal(priced["base_cost"])

    def test_a_bigger_quantity_never_costs_more_per_unit(self):
        sku = pim_oms.search_products("hoodie", category="knits")[0]["sku"]
        assert Decimal(pim_oms.get_price(sku, 500)["unit_price"]) <= Decimal(
            pim_oms.get_price(sku, 50)["unit_price"]
        )

    def test_an_unknown_sku_is_an_error_not_a_guess(self):
        assert "error" in pim_oms.get_price("HL-XXX-9999", 100)

    def test_margin_policy_is_served_not_remembered(self):
        policy = pim_oms.get_margin_policy("knits")
        assert policy["floor_pct"] == "30.0"


class TestSupplier:
    def test_inventory_sums_across_sizes(self):
        sku = pim_oms.search_products("hoodie", category="knits")[0]["sku"]
        rows = supplier.check_inventory(sku, 100)
        assert all("sufficient" in r for r in rows)

    def test_capacity_search_respects_the_window(self):
        found = supplier.find_capacity("screen_print", 100, "2026-09-07", "2026-09-11")
        assert found
        assert all("2026-09-07" <= r["day"] <= "2026-09-11" for r in found)

    def test_a_supplier_is_never_offered_for_a_method_it_lacks(self):
        found = supplier.find_capacity("embroidery", 100, "2026-09-01", "2026-11-01")
        embroiderers = {
            s["id"] for s in supplier.suppliers() if "embroidery" in s["decoration_methods"]
        }
        assert {r["supplier_id"] for r in found} <= embroiderers

    def test_decoration_charges_scale_with_colour_count(self):
        one = supplier.get_decoration_charges("screen_print", 1, 500)
        three = supplier.get_decoration_charges("screen_print", 3, 500)
        assert Decimal(three["setup_fee"]) == Decimal(one["setup_fee"]) * 3
        assert Decimal(three["run_charge_per_unit"]) > Decimal(one["run_charge_per_unit"])

    def test_a_ship_date_clears_both_lead_time_and_capacity(self):
        result = supplier.earliest_ship_date("sup-apex01", "screen_print", 100, "2026-09-07")
        assert "ship_date" in result
        assert result["ship_date"] > "2026-09-07"

    def test_a_method_a_supplier_does_not_offer_is_refused(self):
        assert "error" in supplier.earliest_ship_date(
            "sup-stitch", "screen_print", 100, "2026-09-07"
        )


class TestShipping:
    def test_freight_scales_with_distance(self):
        near = shipping.estimate_freight("IL", 500)
        far = shipping.estimate_freight("CA", 500)
        assert Decimal(far["freight_cost"]) > Decimal(near["freight_cost"])
        assert far["transit_days"] > near["transit_days"]

    def test_delivery_adds_transit_and_skips_weekends(self):
        # 2026-10-02 is a Friday; one transit day lands on the Monday.
        arrives = shipping.delivery_date("2026-10-02", "IL")
        assert arrives["delivery_date"] == "2026-10-05"

    def test_an_unknown_state_is_an_error_not_a_default(self):
        assert "error" in shipping.estimate_freight("ZZ", 100)


def test_decoration_charges_agree_with_the_policy_corpus():
    """A tool and a policy document that quietly disagree is the failure a
    grounding check cannot see."""
    from halo.seed.atlas_sources import CORE_DOCS

    body = next(b for i, _, _, _, b in CORE_DOCS if i == "atl-screen-print-standards")
    charges = supplier.get_decoration_charges("screen_print", 1, 500)

    assert "$22.00 per colour" in body
    assert Decimal(charges["setup_fee"]) == Decimal("22.00")
    assert "$0.85 per piece for the first colour" in body
    assert Decimal(charges["run_charge_per_unit"]) == Decimal("0.85")


def test_inventory_reports_what_each_supplier_can_decorate():
    """Holding the goods and being able to decorate them are different
    capabilities; a caller given only the first picks a supplier that cannot
    do the job."""
    sku = pim_oms.search_products("hoodie", category="knits")[0]["sku"]
    rows = supplier.check_inventory(sku, 100)
    assert rows
    assert all(isinstance(r["decoration_methods"], list) for r in rows)


def test_inventory_filtered_by_method_excludes_warehouses_that_cannot_decorate():
    sku = pim_oms.search_products("hoodie", category="knits")[0]["sku"]
    filtered = supplier.check_inventory(sku, 1, method="screen_print")
    assert all("screen_print" in r["decoration_methods"] for r in filtered)
    assert len(filtered) <= len(supplier.check_inventory(sku, 1))


def test_a_method_refusal_names_who_can_do_it_instead():
    """An agent told only "no" tries another wrong supplier."""
    result = supplier.earliest_ship_date("sup-stitch", "screen_print", 100, "2026-09-07")
    assert "Suppliers that do:" in result["error"]
    assert "Apex Decorating" in result["error"]
