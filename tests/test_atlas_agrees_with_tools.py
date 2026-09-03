"""The margin floors served by the pricing tool must match the Atlas corpus.

A grounding check cannot catch this failure. Retrieval cites a real document, the
tool returns a real number, and the two disagree. Both look valid on their own.
Failing here is better than shipping an answer that contradicts itself.
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
