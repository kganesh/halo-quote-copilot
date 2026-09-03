"""Twenty questions whose answers exist only in the Atlas corpus.

Each carries the chunk that should answer it and a fact that has to survive into
the citation. Both halves are checked separately, because they fail for
different reasons and the fix differs:

  retrieval  — did the right chunk reach the model at all?  (chunking, fusion)
  grounding  — did the answer cite it, quoting text really in it?  (prompt, verify)

The questions are written the way a seller would ask them, not the way the
documents are written. A golden set phrased in the corpus's own vocabulary tests
string matching and calls it retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GoldenQuestion:
    question: str
    expect_chunk: str
    """The chunk that should be retrieved and cited."""
    expect_fact: str
    """A distinctive substring that must appear in the cited quote."""


GOLDEN: list[GoldenQuestion] = [
    GoldenQuestion(
        "What's the most colours we can screen print in one spot?",
        "atl-screen-print-standards#colour-limits",
        "six spot colours",
    ),
    GoldenQuestion(
        "How much do we charge to set up each screen?",
        "atl-screen-print-standards#charges",
        "$22.00 per colour",
    ),
    GoldenQuestion(
        "Customer wants an exact Pantone match — is there a fee?",
        "atl-screen-print-standards#charges",
        "$15.00 per colour",
    ),
    GoldenQuestion(
        "How long does a four colour screen print take to produce?",
        "atl-screen-print-standards#lead-time",
        "10 business days",
    ),
    GoldenQuestion(
        "Do we charge setup again if they reorder the same design next month?",
        "atl-screen-print-standards#setup-waiver",
        "waived",
    ),
    GoldenQuestion(
        "Is embroidery priced by how many thread colours are used?",
        "atl-embroidery-standards#overview",
        "not by colour count",
    ),
    GoldenQuestion(
        "What's the one-time charge to digitise a logo for embroidery?",
        "atl-embroidery-standards#charges",
        "$65.00",
    ),
    GoldenQuestion(
        "How big can a left chest embroidery be?",
        "atl-embroidery-standards#stitch-limits-by-location",
        "15,000 stitches",
    ),
    GoldenQuestion(
        "What's the fastest decoration method we offer?",
        "atl-heat-transfer-standards#lead-time",
        "5 business days",
    ),
    GoldenQuestion(
        "Smallest order we'll take for heat transfer?",
        "atl-heat-transfer-standards#constraints",
        "24 pieces",
    ),
    GoldenQuestion(
        "Can the customer pick the engraving colour on a tumbler?",
        "atl-laser-and-pad-standards#laser-engraving",
        "tone-on-tone",
    ),
    GoldenQuestion(
        "Customer needs it faster than standard — what does that cost?",
        "atl-rush-policy#surcharge",
        "25% to 40%",
    ),
    GoldenQuestion(
        "Can we rush a five colour screen print job?",
        "atl-rush-policy#exclusions",
        "four or more colours",
    ),
    GoldenQuestion(
        "What artwork format do we need for screen printing?",
        "atl-artwork-requirements#accepted-formats",
        "Vector artwork",
    ),
    GoldenQuestion(
        "What's the lowest margin we can quote knits at without approval?",
        "atl-margin-floors#floors-by-category",
        "30.0%",
    ),
    GoldenQuestion(
        "Does freight count towards the margin calculation?",
        "atl-margin-floors#overview",
        "excluded",
    ),
    GoldenQuestion(
        "How many days in transit to a zone 4 address?",
        "atl-freight-and-zones#zone-transit",
        "3 business days",
    ),
    GoldenQuestion(
        "If we ship on the 10th, when does the customer actually get it?",
        "atl-freight-and-zones#promised-ship-date",
        "plus zone transit",
    ),
    GoldenQuestion(
        "Customer wants to see one before we run the whole order — cost and delay?",
        "atl-sampling-policy#overview",
        "$45.00",
    ),
    GoldenQuestion(
        "Can they cancel after we've started printing?",
        "atl-cancellation-policy#after-decoration-begins",
        "non-cancellable",
    ),
]
