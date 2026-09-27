"""The embedding provider port, and a deterministic hashing implementation.

Ingestion and retrieval depend only on `EmbeddingProvider`. A real model
(hosted API or local) will be another implementation of the same port,
chosen later (docs/OPEN_QUESTIONS.md N3); no provider, credential or setting
for one exists yet.

`HashingEmbeddingProvider` is NOT a semantic model. It hashes normalised word
features into a fixed-size vector (the "hashing trick"): texts that share
words point in similar directions, texts that don't are near-orthogonal. It
understands no synonyms or paraphrases. Its purpose is to make ingestion,
pgvector search, and the evaluation harness deterministic and testable
without network access — and to be an honest, measurable baseline.
"""

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

from darwin.db.models.knowledge import EMBEDDING_DIMENSION


class EmbeddingProvider(Protocol):
    """Turns texts into fixed-dimension vectors. Implementations must be pure per input."""

    @property
    def name(self) -> str:
        """Identity recorded on every chunk, e.g. "hashing-bow:v1:384"."""
        ...

    @property
    def dimension(self) -> int: ...

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per input, same order. Batch-friendly."""
        ...


class EmbeddingDimensionError(ValueError):
    pass


def require_dimension(provider: EmbeddingProvider) -> None:
    """The database column is vector(EMBEDDING_DIMENSION); refuse anything else early."""
    if provider.dimension != EMBEDDING_DIMENSION:
        raise EmbeddingDimensionError(
            f"provider {provider.name!r} produces {provider.dimension}-d vectors; "
            f"the schema stores {EMBEDDING_DIMENSION}-d (a change needs a migration and re-embed)"
        )


# Very common English words carry no topic signal.
_STOPWORDS = frozenset(
    """a an and are as at be been but by can do does for from has have how if in into is it
    its of on or so such than that the their then there these they this to was were what when
    where which while who why will with would you your we our not no yes also any all each
    only may must should could""".split()
)
_WORD = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    """A tiny, deterministic suffix stripper (clicks/clicked/clicking -> click)."""
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def features(text: str) -> list[str]:
    """Normalised word features: lowercase, split on non-alphanumerics, drop stopwords, stem."""
    words = [_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1]
    bigrams = [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
    return words + bigrams


class HashingEmbeddingProvider:
    """Deterministic, offline, test/baseline provider (see module docstring)."""

    VERSION = "v1"

    def __init__(self, dimension: int = EMBEDDING_DIMENSION) -> None:
        if dimension < 8:
            raise EmbeddingDimensionError("dimension must be at least 8")
        self._dimension = dimension

    @property
    def name(self) -> str:
        return f"hashing-bow:{self.VERSION}:{self._dimension}"

    @property
    def dimension(self) -> int:
        return self._dimension

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimension
        for feature, count in Counter(features(text)).items():
            # blake2b, not hash(): Python's hash() is randomised per process.
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimension
            sign = 1.0 if digest[4] & 1 else -1.0  # signed hashing reduces collision bias
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]
