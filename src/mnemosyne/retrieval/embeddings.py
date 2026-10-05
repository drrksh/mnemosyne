"""
Embedders: text -> L2-normalized float32 vectors.

Three adapters implement one small interface:

* `HashingEmbedder` (default) — signed feature hashing over code-aware
  tokens, word bigrams, developer *concepts* (see core/vocabulary.py)
  and character n-grams. Deterministic, offline, no model download, and
  stable across processes and machines (it uses BLAKE2b, not Python's
  randomized `hash()`). It captures lexical and morphological similarity
  plus the curated concept layer; it does not capture open-domain
  synonymy the way a neural model does.
* `HttpEmbedder` — any OpenAI-compatible `/embeddings` endpoint: OpenAI,
  Voyage AI, Ollama, vLLM, LM Studio, Azure OpenAI and most gateways.
* `SentenceTransformerEmbedder` — a local model via the optional
  `sentence-transformers` extra.

Every vector is stored tagged with the embedder's `model_id`, so
switching embedders never mixes incompatible vector spaces: the
retrieval layer simply re-embeds whatever is missing for the new model.
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from collections import Counter
from functools import lru_cache
from hashlib import blake2b
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Optional, Sequence

import numpy as np

from mnemosyne.core.text import tokenize
from mnemosyne.core.vocabulary import concepts_for_text, vocabulary_fingerprint
from mnemosyne.domain.exceptions import ConfigurationError, RepositoryError

if TYPE_CHECKING:  # pragma: no cover
    from mnemosyne.config import Settings

logger = logging.getLogger("mnemosyne.retrieval.embeddings")

VECTOR_DTYPE = np.dtype("<f4")


def pack_vector(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=VECTOR_DTYPE).tobytes()


def unpack_vector(blob: bytes, dim: int) -> np.ndarray:
    vector = np.frombuffer(blob, dtype=VECTOR_DTYPE)
    if vector.shape[0] != dim:
        raise ValueError(f"stored vector has {vector.shape[0]} dims, expected {dim}")
    return vector


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim == 1:
        norm = float(np.linalg.norm(matrix))
        return matrix / norm if norm > 0 else matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class Embedder(ABC):
    #: Identifies the vector space. Vectors from different model_ids are
    #: never compared.
    model_id: str
    dim: int

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """(n, dim) float32, rows L2-normalized (zero rows for empty text)."""

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


# ----------------------------------------------------------------------
# Hashing embedder
# ----------------------------------------------------------------------
class HashingEmbedder(Embedder):
    """Signed feature hashing (Weinberger et al., 2009) over a developer-
    aware feature set. Feature weights were tuned against the retrieval
    evaluation suite in `tests/eval/`."""

    VERSION = 1
    WORD_WEIGHT = 1.0
    BIGRAM_WEIGHT = 0.55
    CONCEPT_WEIGHT = 0.8
    NGRAM_WEIGHT = 0.18
    NGRAM_SIZE = 4
    NGRAM_MIN_TOKEN_LEN = 6

    def __init__(self, dim: int = 512) -> None:
        if dim < 32:
            raise ConfigurationError("hashing embedder needs at least 32 dimensions")
        self.dim = dim
        # The vocabulary fingerprint makes any tokenizer or concept change a
        # new vector space; stale stored vectors are then recomputed.
        self.model_id = f"hashing-v{self.VERSION}-{dim}-{vocabulary_fingerprint()}"

    def features(self, text: str) -> dict[str, float]:
        tokens = tokenize(text)
        if not tokens:
            return {}
        weighted: dict[str, float] = {}

        def add(counter: Counter[str], weight: float) -> None:
            for feature, tf in counter.items():
                weighted[feature] = weighted.get(feature, 0.0) + weight * (1.0 + math.log(tf))

        add(Counter("w:" + t for t in tokens), self.WORD_WEIGHT)
        add(Counter(f"b:{a}_{b}" for a, b in pairwise(tokens)), self.BIGRAM_WEIGHT)
        add(Counter("c:" + c for c in concepts_for_text(text)), self.CONCEPT_WEIGHT)
        grams: Counter[str] = Counter()
        for token in set(tokens):
            if len(token) >= self.NGRAM_MIN_TOKEN_LEN and token.isalpha():
                padded = f"<{token}>"
                grams.update("g:" + padded[i : i + self.NGRAM_SIZE] for i in range(len(padded) - self.NGRAM_SIZE + 1))
        add(grams, self.NGRAM_WEIGHT)
        return weighted

    def embed_one(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        for feature, weight in self.features(text).items():
            index, sign = _hash_feature(feature, self.dim)
            vector[index] += sign * weight
        return l2_normalize(vector)

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self.embed_one(t) for t in texts])


@lru_cache(maxsize=262144)
def _hash_feature(feature: str, dim: int) -> tuple[int, float]:
    digest = int.from_bytes(blake2b(feature.encode("utf-8"), digest_size=8).digest(), "little")
    return digest % dim, (1.0 if (digest >> 63) & 1 else -1.0)


# ----------------------------------------------------------------------
# OpenAI-compatible HTTP embedder
# ----------------------------------------------------------------------
class HttpEmbedder(Embedder):
    def __init__(
        self,
        *,
        url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout_seconds: float = 30.0,
        batch_size: int = 64,
        dim: Optional[int] = None,
        client: Any = None,
    ) -> None:
        import httpx

        self._url = url
        self._model = model
        self._batch_size = max(1, batch_size)
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self.model_id = f"http:{model}"
        self.dim = dim or 0

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        rows: list[np.ndarray] = []
        for start in range(0, len(texts), self._batch_size):
            batch = [t if t.strip() else " " for t in texts[start : start + self._batch_size]]
            rows.append(self._request(batch))
        if not rows:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack(rows)

    def _request(self, batch: list[str]) -> np.ndarray:
        try:
            response = self._client.post(
                self._url, headers=self._headers, json={"model": self._model, "input": batch}
            )
            response.raise_for_status()
            payload = response.json()
            data = sorted(payload["data"], key=lambda item: item.get("index", 0))
            matrix = np.asarray([item["embedding"] for item in data], dtype=np.float32)
        except Exception as exc:  # network, HTTP status, or malformed payload
            raise RepositoryError(f"embedding request failed: {type(exc).__name__}") from exc
        if matrix.shape[0] != len(batch):
            raise RepositoryError("embedding response size mismatch")
        if not self.dim:
            self.dim = int(matrix.shape[1])
        elif matrix.shape[1] != self.dim:
            raise RepositoryError(f"embedding dimension changed: {matrix.shape[1]} != {self.dim}")
        return l2_normalize(matrix)


# ----------------------------------------------------------------------
# Local sentence-transformers embedder
# ----------------------------------------------------------------------
class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model_name: str, batch_size: int = 64) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigurationError(
                "embedder=sentence-transformers requires: pip install 'mnemosyne[local-embeddings]'"
            ) from exc
        self._model = SentenceTransformer(model_name)  # pragma: no cover
        self._batch_size = batch_size  # pragma: no cover
        self.dim = int(self._model.get_sentence_embedding_dimension())  # pragma: no cover
        self.model_id = f"st:{model_name}"  # pragma: no cover

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:  # pragma: no cover - optional extra
        matrix = self._model.encode(
            list(texts), batch_size=self._batch_size, normalize_embeddings=True, show_progress_bar=False
        )
        return np.asarray(matrix, dtype=np.float32)


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedder == "hashing":
        return HashingEmbedder(dim=settings.embedding_dim)
    if settings.embedder == "http":
        key = settings.embedding_http_api_key.get_secret_value() if settings.embedding_http_api_key else None
        return HttpEmbedder(
            url=settings.embedding_http_url,
            model=settings.embedding_http_model,
            api_key=key,
            timeout_seconds=settings.embedding_http_timeout_seconds,
            batch_size=settings.embedding_batch_size,
        )
    if settings.embedder == "sentence-transformers":
        return SentenceTransformerEmbedder(settings.embedding_local_model, settings.embedding_batch_size)
    raise ConfigurationError(f"unknown embedder {settings.embedder!r}")  # pragma: no cover
