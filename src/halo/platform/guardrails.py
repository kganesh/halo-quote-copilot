"""M4: the check that runs on what goes into a model and what comes out of it.

The envelope makes untrusted content *look* like data. This module decides
whether a piece of text is allowed through at all. They are different jobs and
both are needed: labelling a hostile note as evidence does not stop the answer
from carrying a 40% discount, and blocking the word "discount" does not stop a
note from impersonating the operator.

Three categories, taken from the milestone:

- **PII.** A customer's email, phone or card number must not come back out in a
  quote. On the way in it is reported and allowed, because a real request from a
  seller often carries a contact address and refusing it would break the product.
  On the way out it blocks. Asymmetry is the point.
- **Denied topics.** Discount authority and legal commitments. An agent may
  report what a policy says about either; it may not commit HALO to one.
- **Prompt injection.** Text addressed to the model rather than about the goods.

There are two implementations, following `ToolGateway` and `ModelClient`.
`BedrockGuardrail` calls the real ApplyGuardrail API. `LocalGuardrail` applies
the same categories with patterns, in process. The offline suite uses the local
one, so the red-team set runs in CI without an AWS account and without spending
anything. The category names are shared, so a run against either surface
produces the same verdict vocabulary.

A local pattern set is not as good as the managed one and is not meant to be.
It is a floor, not a ceiling: what it catches, Bedrock also catches. The reason
it exists is that a safety check nothing exercises in CI is a safety check
nobody notices breaking.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class Surface(StrEnum):
    """Which side of the model the text is on. Matches ApplyGuardrail's `source`."""

    INPUT = "input"
    OUTPUT = "output"


class Category(StrEnum):
    PII = "pii"
    DISCOUNT_COMMITMENT = "denied_topic:discount_commitment"
    LEGAL_COMMITMENT = "denied_topic:legal_commitment"
    PROMPT_INJECTION = "prompt_injection"
    UNGROUNDED = "grounding"


@dataclass(frozen=True)
class GuardrailVerdict:
    """What the guardrail found, and whether that stops the run.

    `categories` is populated even when nothing is blocked. A note that tried an
    injection and was allowed through as inert evidence is still worth recording,
    and the red-team report counts those separately from the ones that blocked.
    """

    surface: Surface
    blocked: bool = False
    categories: tuple[Category, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.categories

    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "no guardrail finding"


class Guardrail(Protocol):
    def inspect(
        self,
        text: str,
        *,
        surface: Surface,
        grounding_source: str | None = None,
    ) -> GuardrailVerdict: ...


PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"),
    re.compile(r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b"),
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),
)
"""Email, phone, card. Shared with the event envelope on purpose.

What the guardrail refuses to say in an answer is what the record refuses to
keep. Two lists would drift, and the drift would be invisible: the guardrail is
exercised by twenty red-team notes on every build, and a redaction rule nobody
tests is found by an auditor."""


# Each rule is (category, pattern, surfaces it blocks on). A rule that matches on
# a surface it does not block on is still reported. `re.VERBOSE` is not used, so
# that a pattern can be copied straight into a Bedrock topic definition.
_RULES: list[tuple[Category, re.Pattern[str], frozenset[Surface]]] = [
    *((Category.PII, pattern, frozenset({Surface.OUTPUT})) for pattern in PII_PATTERNS),
    (
        Category.DISCOUNT_COMMITMENT,
        re.compile(
            r"\b\d{1,3}\s?%\s?(?:off|discount|reduction)"
            r"|\bdiscount(?:ed)?\b(?=[^.]*\b(?:appl|giv|offer|grant|honou?r|extend)\w*)"
            # A waiver is blocked when we are the one granting it, in either word
            # order: "we waive the setup fee" and "margin floor waived on this
            # order" are the same commitment, and the red-team set found the
            # second form getting through a verb-first rule.
            #
            # Reporting a waiver is not granting one. "Policy waives the setup
            # fee on an exact reorder" is the correct answer to a real question
            # and has to survive, so the rule looks for the actor or the scope,
            # not for the word on its own.
            r"|\b(?:we|halo)\b[^.]{0,40}\bwaiv\w+"
            r"|\bwaiv\w+\b[^.]{0,40}\b(?:on|for)\s+(?:this|the)\s+"
            r"(?:order|quote|job|account|customer)\b"
            r"|\b(?:free of charge|at no (?:cost|charge)|no additional charge)\b"
            r"|\bprice[ -]match\w*\b",
            re.IGNORECASE,
        ),
        frozenset({Surface.OUTPUT}),
    ),
    (
        Category.LEGAL_COMMITMENT,
        re.compile(
            r"\bindemnif\w*\b"
            r"|\b(?:we|halo)\s+(?:warrant|guarantee)s?\b"
            r"|\b(?:legally |contractually )?binding\b"
            r"|\bhold\s+(?:you|the customer|them)\s+harmless\b"
            r"|\bliab\w+\s+for\b"
            r"|\bmoney[- ]back guarantee\b",
            re.IGNORECASE,
        ),
        frozenset({Surface.OUTPUT}),
    ),
    (
        Category.PROMPT_INJECTION,
        re.compile(
            r"\bignore\b[^.]*\b(?:previous|prior|earlier|above|all)\b[^.]*\b(?:instruction|prompt|rule)s?\b"
            r"|\bdisregard\b[^.]*\b(?:instruction|prompt|rule|polic)\w*"
            r"|\b(?:system|assistant)\s*(?:prompt|message|note)\s*[:>]"
            r"|^\s*(?:system|assistant)\s*:"
            r"|\byou (?:are|must) now\b"
            r"|\bnew instructions?\b"
            r"|\boverrid\w+\b[^.]*\b(?:polic|check|rule|verification)\w*"
            r"|\bdo not (?:mention|report|include|tell|disclose)\b"
            r"|\b(?:skip|bypass|omit)\w*\b[^.]*\b(?:verification|check|citation|approval|step)s?\b"
            r"|\b(?:verification|check|citation|approval)s?\b[^.]*\b(?:skipp?ed|bypassed|omitted)\b"
            r"|\breply only with\b"
            r"|\bmarker removed\b",
            re.IGNORECASE | re.MULTILINE,
        ),
        frozenset({Surface.INPUT, Surface.OUTPUT}),
    ),
]

_SENTENCE = re.compile(r"[^.!?\n]+")


class LocalGuardrail:
    """The same categories, applied in process. No network, no account, no cost."""

    name = "local"

    def inspect(
        self,
        text: str,
        *,
        surface: Surface,
        grounding_source: str | None = None,
    ) -> GuardrailVerdict:
        categories: list[Category] = []
        reasons: list[str] = []
        blocked = False

        for category, pattern, blocking_on in _RULES:
            match = pattern.search(text)
            if match is None:
                continue
            if category not in categories:
                categories.append(category)
                reasons.append(f"{category} matched {_excerpt(match)!r}")
            if surface in blocking_on:
                blocked = True

        # Contextual grounding, in the one form a local check can honestly do:
        # if a source was supplied, every sentence of the output has to have some
        # lexical footing in it. This is a weaker test than the Bedrock policy's
        # score and is meant only to catch an answer that left the source behind
        # entirely.
        if (
            grounding_source is not None
            and surface is Surface.OUTPUT
            and (ungrounded := _ungrounded_sentences(text, grounding_source))
        ):
            categories.append(Category.UNGROUNDED)
            reasons.append(f"not supported by the source: {ungrounded[0]!r}")
            blocked = True

        return GuardrailVerdict(
            surface=surface,
            blocked=blocked,
            categories=tuple(categories),
            reasons=tuple(reasons),
        )


class BedrockGuardrail:
    """Amazon Bedrock's ApplyGuardrail, called directly.

    ApplyGuardrail is used rather than attaching the guardrail to the model call,
    because the text that most needs checking is not always part of a model call:
    a tool result is inspected before it is ever put in a prompt.

    Created out of band, not by this code. See `docs/aws-setup.md`; M8 moves it
    into Terraform with the rest of the account.
    """

    name = "bedrock"

    def __init__(
        self,
        *,
        guardrail_id: str | None = None,
        version: str | None = None,
        region: str | None = None,
    ) -> None:
        import boto3

        self.guardrail_id = guardrail_id or os.environ.get("HALO_GUARDRAIL_ID", "")
        self.version = version or os.environ.get("HALO_GUARDRAIL_VERSION", "DRAFT")
        if not self.guardrail_id:
            raise ValueError(
                "no guardrail id: pass guardrail_id, or set HALO_GUARDRAIL_ID. "
                "docs/aws-setup.md has the create-guardrail command."
            )
        self._client = boto3.client(
            "bedrock-runtime", region_name=region or os.environ.get("AWS_REGION", "us-east-1")
        )

    def inspect(
        self,
        text: str,
        *,
        surface: Surface,
        grounding_source: str | None = None,
    ) -> GuardrailVerdict:
        content: list[dict[str, Any]] = [{"text": {"text": text, "qualifiers": ["guard_content"]}}]
        if grounding_source is not None:
            content.append({"text": {"text": grounding_source, "qualifiers": ["grounding_source"]}})

        response = self._client.apply_guardrail(
            guardrailIdentifier=self.guardrail_id,
            guardrailVersion=self.version,
            source=surface.value.upper(),
            content=content,
        )

        categories, reasons = _read_assessments(response.get("assessments", []))
        return GuardrailVerdict(
            surface=surface,
            blocked=response.get("action") == "GUARDRAIL_INTERVENED",
            categories=tuple(categories),
            reasons=tuple(reasons),
        )


def _read_assessments(assessments: list[dict]) -> tuple[list[Category], list[str]]:
    """Map an ApplyGuardrail response onto this module's categories.

    Every branch reads defensively. The response shape is versioned by AWS and
    an unknown policy block should cost us a category name, not raise inside a
    safety check.
    """
    categories: list[Category] = []
    reasons: list[str] = []

    def add(category: Category, reason: str) -> None:
        if category not in categories:
            categories.append(category)
        reasons.append(reason)

    for assessment in assessments:
        for topic in assessment.get("topicPolicy", {}).get("topics", []):
            if topic.get("action") != "BLOCKED":
                continue
            name = str(topic.get("name", "")).lower()
            category = (
                Category.LEGAL_COMMITMENT
                if "legal" in name or "contract" in name
                else Category.DISCOUNT_COMMITMENT
            )
            add(category, f"denied topic {topic.get('name')!r}")

        pii = assessment.get("sensitiveInformationPolicy", {})
        for entity in pii.get("piiEntities", []) + pii.get("regexes", []):
            add(Category.PII, f"pii {entity.get('type') or entity.get('name')}")

        for filt in assessment.get("contentPolicy", {}).get("filters", []):
            if filt.get("type") == "PROMPT_ATTACK":
                add(Category.PROMPT_INJECTION, "prompt attack filter")

        for filt in assessment.get("contextualGroundingPolicy", {}).get("filters", []):
            if filt.get("action") == "BLOCKED":
                add(Category.UNGROUNDED, f"{filt.get('type', 'grounding')} below threshold")

    return categories, reasons


def _excerpt(match: re.Match[str], width: int = 60) -> str:
    text = match.group(0).strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _ungrounded_sentences(text: str, source: str, floor: float = 0.4) -> list[str]:
    """Sentences whose content words are mostly absent from the source."""
    source_words = _words(source)
    ungrounded = []
    for sentence in _SENTENCE.findall(text):
        words = _words(sentence)
        if len(words) < 6:
            continue  # Too short to judge; "Yes." is not a grounding failure.
        overlap = len(words & source_words) / len(words)
        if overlap < floor:
            ungrounded.append(sentence.strip())
    return ungrounded


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9$.]+", text.lower()) if len(word) > 2}
