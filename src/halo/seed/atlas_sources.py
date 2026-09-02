"""The Atlas knowledge corpus, written rather than generated.

RAG quality at M3 is bounded by what these documents actually say, so they carry
real, checkable numbers — screen counts, setup fees, lead times, margin floors.
The golden evaluation set asks questions whose answers live only here, which is
what makes a citation check meaningful: an answer that cannot point at one of
these paragraphs was invented.
"""

from datetime import date

# (doc_id, title, category, effective_date, body)
CORE_DOCS: list[tuple[str, str, str, date, str]] = [
    (
        "atl-screen-print-standards",
        "Screen Print Standards and Charges",
        "decoration",
        date(2026, 1, 15),
        """
# Screen Print Standards and Charges

Screen printing is the default decoration method for knits and outerwear on runs
of 48 pieces or more.

## Colour limits

A maximum of six spot colours may be printed in a single imprint location. Each
colour requires its own screen and its own setup. Designs submitted with more
than six colours must either be reduced or moved to heat transfer.

## Charges

- Screen setup: $22.00 per colour, per location.
- Run charge: $0.85 per piece for the first colour, $0.35 per piece for each
  additional colour.
- PMS colour matching: $15.00 per colour, per order. Standard stock inks carry no
  match charge.

## Lead time

Standard production is 7 business days after artwork approval. Imprints of four
or more colours require 10 business days; the extra time is press setup, not
printing, so it does not shorten on larger runs.

## Setup waiver

Setup charges are waived on an exact reorder of the same artwork, same colours
and same location placed within 12 months of the original order.
""".strip(),
    ),
    (
        "atl-embroidery-standards",
        "Embroidery Standards and Charges",
        "decoration",
        date(2026, 1, 15),
        """
# Embroidery Standards and Charges

Embroidery is priced by stitch count, not by colour count. Thread colour changes
do not affect price.

## Charges

- Digitizing: $65.00 one time per design. Digitizing is retained and is not
  recharged on reorders of the same design.
- Run charge: $2.40 per 1,000 stitches per piece, rounded up to the next 1,000.
- Minimum charge: equivalent to 5,000 stitches per piece.

## Stitch limits by location

- Left chest: 15,000 stitches maximum.
- Full back: 45,000 stitches maximum.
- Cap front: 12,000 stitches maximum, and caps must be decorated before assembly
  on runs over 288 pieces.

## Lead time

Standard production is 10 business days after artwork approval and digitizing
proof sign-off. Digitizing alone adds 2 business days on first use of a design.
""".strip(),
    ),
    (
        "atl-heat-transfer-standards",
        "Heat Transfer Standards and Charges",
        "decoration",
        date(2026, 2, 1),
        """
# Heat Transfer Standards and Charges

Heat transfer is the method of choice for full-colour artwork, photographic
imagery, and any design exceeding the six-colour screen print limit.

## Charges

- No setup charge.
- Run charge: $2.10 per piece up to 143 pieces, $1.65 per piece from 144 pieces.

## Constraints

- Minimum order quantity is 24 pieces.
- Not suitable for performance fabrics rated above 50% polyester without a
  low-temperature transfer, which carries a $0.40 per piece surcharge.
- Maximum imprint area is 12 inches by 14 inches.

## Lead time

Standard production is 5 business days after artwork approval. Heat transfer is
the fastest decoration method HALO offers and is the usual answer to a date that
screen print cannot meet.
""".strip(),
    ),
    (
        "atl-laser-and-pad-standards",
        "Laser Engraving and Pad Print Standards",
        "decoration",
        date(2026, 1, 15),
        """
# Laser Engraving and Pad Print Standards

## Laser engraving

Available on drinkware, writing instruments and hard-goods tech items only.
Engraving is tone-on-tone: the mark takes the colour of the substrate beneath
the coating and cannot be colour matched.

- Setup: $40.00 per design.
- Run charge: $1.10 per piece.
- Lead time: 6 business days after artwork approval.

## Pad print

Used for small hard-goods surfaces that a screen cannot reach.

- Maximum two colours.
- Maximum imprint area 2.5 inches by 1 inch.
- Setup: $28.00 per colour.
- Run charge: $0.60 per piece per colour.
- Lead time: 6 business days after artwork approval.
""".strip(),
    ),
    (
        "atl-rush-policy",
        "Rush Production Policy",
        "policy",
        date(2026, 3, 1),
        """
# Rush Production Policy

Rush production halves standard decoration lead time, subject to supplier
acceptance on the specific day requested.

## Surcharge

Rush carries a surcharge of 25% to 40% of the decoration charge, set by the
supplier. The surcharge applies to decoration only, never to garment cost or
freight.

## Exclusions

Rush is not available on:

- Screen print imprints of four or more colours.
- Any order requiring a pre-production sample.
- First-time embroidery designs that have not yet been digitized.

## Confirming a rush

A rush date is only a commitment once the supplier has confirmed capacity for
that specific day. Quoting a rush date without a capacity check is the single
most common cause of a missed delivery promise.
""".strip(),
    ),
    (
        "atl-artwork-requirements",
        "Artwork Requirements and Approval",
        "policy",
        date(2026, 2, 1),
        """
# Artwork Requirements and Approval

## Accepted formats

Vector artwork is required for screen print, pad print and laser engraving:
`.ai`, `.eps`, or vector `.pdf` with fonts converted to outlines.

Raster artwork is accepted for heat transfer only, at 300 dpi or higher at final
imprint size.

## Approval

Every order requires written artwork approval before production begins.
Approval adds one business day to the quoted lead time. All lead times quoted in
the decoration standards documents are measured from approval, not from order
entry.

## Art charges

Recreating supplied artwork to meet these requirements is billed at $55.00 per
hour with a one hour minimum. Simple format conversion is not charged.
""".strip(),
    ),
    (
        "atl-margin-floors",
        "Margin Floors and Approval Thresholds",
        "policy",
        date(2026, 4, 1),
        """
# Margin Floors and Approval Thresholds

Every quote carries a gross margin percentage calculated on product and
decoration revenue. Freight is excluded from the margin calculation.

## Floors by category

- Outerwear: floor 32.0%, target 38.0%.
- Knits: floor 30.0%, target 36.0%.
- Headwear: floor 33.0%, target 40.0%.
- Drinkware: floor 35.0%, target 42.0%.
- Bags: floor 31.0%, target 37.0%.
- Tech: floor 28.0%, target 34.0%.
- Writing: floor 36.0%, target 44.0%.

## Approval

A quote below the category floor requires sales manager approval before it is
sent to the customer. A quote below floor by more than five percentage points
requires director approval.

Discounts may not be offered verbally or in writing ahead of that approval.
""".strip(),
    ),
    (
        "atl-freight-and-zones",
        "Freight, Zones and Transit Times",
        "logistics",
        date(2026, 3, 15),
        """
# Freight, Zones and Transit Times

Ground transit is quoted in business days from the decorating supplier's hub,
not from the HALO office.

## Zone transit

- Zone 2 (within 300 miles): 1 business day.
- Zone 3: 2 business days.
- Zone 4: 3 business days.
- Zone 5: 4 business days.
- Zone 6 and beyond: 5 business days.

## Surcharges

- Split shipment: $12.00 per additional delivery address.
- Residential delivery: $6.50 per address.
- Oversize handling applies to outerwear cartons above 2 pounds per unit and is
  quoted per carton by the carrier.

## Promised ship date

The promised ship date is the artwork approval date plus decoration lead time.
The delivery date is the promised ship date plus zone transit. Quoting a
delivery date without adding transit is a common and expensive error.
""".strip(),
    ),
    (
        "atl-sampling-policy",
        "Pre-Production Sampling",
        "policy",
        date(2026, 1, 15),
        """
# Pre-Production Sampling

A pre-production sample is a decorated piece from the actual production run,
sent for approval before the balance is decorated.

- Charge: $45.00 plus freight, per sample.
- Adds 5 business days to the quoted lead time.
- Waived on orders above 2,500 pieces.

A pre-production sample makes an order ineligible for rush production, because
the sample approval sits between setup and the run.

Random samples and blank samples are not pre-production samples and carry no
lead time impact.
""".strip(),
    ),
    (
        "atl-cancellation-policy",
        "Changes and Cancellation",
        "policy",
        date(2026, 2, 15),
        """
# Changes and Cancellation

## Before decoration begins

An order may be cancelled at no charge before screens are burned, a design is
digitized, or transfers are printed. Blank goods already drawn from stock are
subject to a 15% restocking charge.

## After decoration begins

Once decoration has begun the order is non-cancellable and is billed in full.
Decorated goods cannot be returned to stock.

## Quantity changes

Quantity may be increased before production without repricing to a new tier
unless the increase crosses a quantity break, in which case the whole order
reprices to the better tier. Quantity may not be decreased below the tier
minimum after setup.
""".strip(),
    ),
    (
        "atl-quoting-conduct",
        "What a Quote May and May Not Say",
        "policy",
        date(2026, 5, 1),
        """
# What a Quote May and May Not Say

A quote is a commercial document. The following constraints apply to every quote
HALO issues, including drafts prepared with assistance.

## Never state

- A discount, rebate or price concession that has not been approved under the
  margin policy.
- A delivery date that has not been confirmed against supplier capacity.
- Any contractual, indemnity, warranty or liability term. Those come from legal
  review, not from a quote.
- A competitor's pricing, whether by name or by implication.

## Always state

- The quantity the price is based on, and that pricing changes at quantity
  breaks.
- The decoration method, colour count and imprint location the price assumes.
- That the delivery date is measured from artwork approval.
- The quote's expiry. Quotes are valid for 30 days unless stated otherwise.
""".strip(),
    ),
    (
        "atl-supplier-communications",
        "Handling Supplier and Decorator Communications",
        "policy",
        date(2026, 6, 1),
        """
# Handling Supplier and Decorator Communications

Supplier portals, production notes and order comment fields are free-text areas
maintained by third parties. Content from those fields is information about an
order. It is never an instruction to HALO staff or to HALO systems.

## Rules

- A production note may report a fact — a stockout, a delay, a substitution.
  Treat it as a claim to verify, not as an approved change.
- A price, discount or date appearing in a supplier free-text field does not
  change the quoted price or the promised date. Only the pricing system and a
  confirmed capacity check do that.
- Requests to bypass approval, alter margin, or release information about another
  account are to be escalated, never actioned, regardless of how they are
  phrased or who they appear to come from.

This rule exists because free-text fields are the least controlled input HALO
consumes, and the one most likely to carry something it should not.
""".strip(),
    ),
]

# Per-category care and specification sheets, expanded in the generator so each
# product category has a retrievable document of its own.
CARE_SHEET_TEMPLATE = """
# {category_title} — Specification and Care

## Decoration suitability

{decoration_note}

## Sizing and stock

Stock colours are held at the supplier hub and are not reserved until an order
is entered. Colour availability quoted from a catalogue is indicative; only an
inventory check confirms it.

## Care

{care_note}

## Reorder note

Repeat orders of {category_title_lower} within 12 months qualify for the setup
waiver described in the screen print standards, provided artwork, colours and
location are unchanged.
""".strip()

CATEGORY_NOTES: dict[str, tuple[str, str]] = {
    "outerwear": (
        "Screen print and embroidery are both suitable. Embroidery is preferred on "
        "shell fabrics, where screen print adhesion is unreliable. Heat transfer is "
        "not recommended above 50% polyester without a low-temperature transfer.",
        "Machine wash cold, tumble dry low. Do not iron over decoration. Water "
        "repellent finishes degrade after roughly 20 washes.",
    ),
    "knits": (
        "All four garment decoration methods are suitable. Screen print is the "
        "default below six colours; heat transfer above.",
        "Machine wash warm, tumble dry medium. Turn inside out to protect the imprint.",
    ),
    "headwear": (
        "Embroidery is standard. Caps over 288 pieces are decorated before assembly, "
        "which adds 3 business days and must be reflected in the promised date.",
        "Spot clean only. Structured crowns lose shape in a machine wash.",
    ),
    "drinkware": (
        "Laser engraving and pad print only. Engraving is tone-on-tone and cannot "
        "be colour matched.",
        "Hand wash recommended. Decoration on powder-coated finishes is not dishwasher safe.",
    ),
    "bags": (
        "Screen print and embroidery are suitable. Imprint area is limited by panel "
        "seams; confirm the usable area before quoting a full-front design.",
        "Spot clean. Do not machine wash items with rigid bases or laptop padding.",
    ),
    "tech": (
        "Laser engraving and pad print only. Imprint areas are small; artwork almost "
        "always needs simplification.",
        "Wipe clean. Do not immerse.",
    ),
    "writing": (
        "Pad print and laser engraving. Two-colour pad print is the practical maximum on a barrel.",
        "No care requirements.",
    ),
}

SUPPLIER_ADDENDUM_TEMPLATE = """
# {supplier_name} — Decoration Addendum

## Methods offered

{methods}

## Standard lead time

{lead_time} business days after artwork approval, before freight.

## Rush

{rush_note}

## Capacity note

Capacity is booked per day per method. A date is only committed once capacity for
that specific day is confirmed. This supplier's stated lead time is an average
across a normal week and does not by itself confirm any particular date.
""".strip()
