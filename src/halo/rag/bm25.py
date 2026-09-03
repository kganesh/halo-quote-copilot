"""BM25 over the Atlas corpus. This is the keyword half of retrieval.

This is written out rather than imported from a library, for two reasons. It is
the part of hybrid search that is worth understanding. And what it catches is
specific: product codes, dollar amounts, "PMS", "digitizing", "2XL". A vector
search knows those terms relate to pricing and decoration. It is not reliable
about which exact one you typed.

The parameters are the standard values. `k1` controls how quickly repeated terms
stop adding to the score. `b` controls how much a long document is penalised for
its length.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

K1 = 1.5
B = 0.75

_TOKEN = re.compile(r"[a-z0-9$.%-]+")

_STOPWORD_TEXT = """
a an the is are was were be been being of for to in on at by with from
and or but if then than that this these those it its as no not do does did
what when which who whom how why can could should would will shall may might
must have has had i you we they me my our your their
"""

STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def tokenize(text: str) -> list[str]:
    """Lowercase tokens, keeping the characters that carry meaning in this corpus.

    `$22.00`, `2xl` and `base.en` stay intact. A tokenizer that split on
    punctuation would turn `$22.00` into `22` and `00`, which loses the fact that
    the question was about a price.
    """
    return [
        token.strip(".")
        for token in _TOKEN.findall(text.lower())
        if token.strip(".") and token.strip(".") not in STOPWORDS
    ]


@dataclass
class Bm25Index:
    ids: list[str] = field(default_factory=list)
    _docs: list[Counter] = field(default_factory=list)
    _lengths: list[int] = field(default_factory=list)
    _df: Counter = field(default_factory=Counter)
    _avg_length: float = 0.0

    @classmethod
    def build(cls, documents: dict[str, str]) -> Bm25Index:
        index = cls()
        for doc_id, text in documents.items():
            tokens = tokenize(text)
            index.ids.append(doc_id)
            index._docs.append(Counter(tokens))
            index._lengths.append(len(tokens))
            index._df.update(set(tokens))
        index._avg_length = sum(index._lengths) / len(index._lengths) if index._lengths else 0.0
        return index

    def _idf(self, term: str) -> float:
        n = len(self.ids)
        df = self._df.get(term, 0)
        # The +0.5 smoothing keeps a term that appears in every document at a
        # small positive weight instead of a negative one.
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, limit: int = 10) -> list[tuple[str, float]]:
        terms = tokenize(query)
        if not terms or not self.ids:
            return []

        scored: list[tuple[str, float]] = []
        for position, doc_id in enumerate(self.ids):
            counts = self._docs[position]
            length = self._lengths[position]
            score = 0.0
            for term in terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                denominator = frequency + K1 * (1 - B + B * length / (self._avg_length or 1))
                score += self._idf(term) * (frequency * (K1 + 1)) / denominator
            if score > 0:
                scored.append((doc_id, score))

        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:limit]
