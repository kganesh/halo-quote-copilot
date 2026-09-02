"""The margin floors the pricing tool will serve must match the ones the Atlas
corpus states.

This is the failure that grounding checks cannot catch: retrieval cites a real
document, the tool returns a real number, and the two disagree. Better to fail
here than to ship an answer that is correct and contradicts itself.
"""

import re
from decimal import Decimal

from conftest import load


def test_margin_floors_match_the_policy_document(corpus):
    doc = next(d for d in load(corpus, "atlas_docs") if d["id"] == "atl-margin-floors")
    stated = {
        m.group(1).lower(): (Decimal(m.group(2)), Decimal(m.group(3)))
        for m in re.finditer(r"- (\w+): floor ([\d.]+)%, target ([\d.]+)%\.", doc["body"])
    }
    assert stated, "policy document format changed — the parser needs updating"

    for policy in load(corpus, "margin_policies"):
        floor, target = stated[policy["category"]]
        assert Decimal(str(policy["floor_pct"])) == floor
        assert Decimal(str(policy["target_pct"])) == target


def test_supplier_addenda_state_the_real_lead_time(corpus):
    suppliers = {s["id"]: s for s in load(corpus, "suppliers")}
    docs = {d["id"]: d for d in load(corpus, "atlas_docs")}
    for supplier_id, supplier in suppliers.items():
        doc = docs[f"atl-supplier-{supplier_id[4:]}"]
        assert f"{supplier['base_lead_time_days']} business days" in doc["body"]
