"""Titan Text Embeddings v2 on Bedrock.

This is an Amazon model, not an Anthropic one. It sits behind a different
Bedrock agreement, so it was usable on this account while the newer Claude tiers
were still blocked.

The vectors come back normalised to unit length. Cosine similarity is therefore a
dot product, and the store does not need to keep or divide by a magnitude.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Protocol

MODEL_ID = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 1024
PRICE_PER_MTOK = Decimal("0.02")
"""Titan v2 input price per million tokens.

Embedding the whole Atlas corpus costs a small fraction of a cent. That is why
ingest re-embeds everything instead of tracking which documents changed.
"""


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    @property
    def tokens_used(self) -> int: ...


class TitanEmbedder:
    """Returns one vector per input, in the same order. Titan accepts one text
    per call."""

    def __init__(self, region: str = "us-east-1", model_id: str = MODEL_ID) -> None:
        import boto3

        self._client = boto3.client("bedrock-runtime", region_name=region)
        self.model_id = model_id
        self._tokens = 0

    @property
    def tokens_used(self) -> int:
        return self._tokens

    @property
    def usd(self) -> Decimal:
        return (Decimal(self._tokens) / Decimal(1_000_000) * PRICE_PER_MTOK).quantize(
            Decimal("0.000001")
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            response = self._client.invoke_model(
                modelId=self.model_id,
                body=json.dumps({"inputText": text, "dimensions": DIMENSIONS, "normalize": True}),
            )
            payload = json.loads(response["body"].read())
            self._tokens += payload.get("inputTextTokenCount", 0)
            vectors.append(payload["embedding"])
        return vectors
