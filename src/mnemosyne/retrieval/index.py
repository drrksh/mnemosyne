"""
RetrievalIndex: an immutable, query-ready snapshot of one principal's
active memories (lexical index + embedding matrix), and IndexCache,
which reuses snapshots for as long as the database says nothing changed.

Retrieval is always per-principal (personal memories plus their teams'
memories), so each snapshot covers a bounded corpus — typically
hundreds to tens of thousands of memories — and brute-force cosine over
a contiguous float32 matrix is both exact and fast at that size. A
larger deployment would swap this module for an ANN index behind the
same `search_vectors` contract; nothing above it would change.
"""
from __future__ import annotations

import hashlib
import logging
import os
import pickle
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import numpy as np

from mnemosyne.core.vocabulary import concept_weight
from mnemosyne.domain.entities import Memory
from mnemosyne.repositories.interfaces import IndexSignature
from mnemosyne.retrieval.bm25 import Bm25Index
from mnemosyne.retrieval.documents import CONCEPT_PREFIX, memory_fields, query_concepts, query_terms

logger = logging.getLogger("mnemosyne.retrieval.index")


@dataclass
class RetrievalIndex:
    model_id: str
    memories: list[Memory]
    #: (n, dim) float32, L2-normalized rows; zero rows where no embedding.
    vectors: np.ndarray
    has_vector: np.ndarray
    bm25: Bm25Index
    position: dict[UUID, int] = field(default_factory=dict)

    @classmethod
    def build(
        cls, *, model_id: str, memories: list[Memory], vectors: np.ndarray, has_vector: np.ndarray
    ) -> RetrievalIndex:
        return cls(
            model_id=model_id,
            memories=memories,
            vectors=vectors,
            has_vector=has_vector,
            bm25=Bm25Index.build(memory_fields(m) for m in memories),
            position={m.id: i for i, m in enumerate(memories)},
        )

    @property
    def size(self) -> int:
        return len(self.memories)

    @property
    def id_rank(self) -> np.ndarray:
        """Each memory's rank by id: a tie-break that does not depend on the
        order rows happened to be loaded in, so equal scores order the same
        way in every process."""
        cached = self.__dict__.get("_id_rank")
        if cached is None or len(cached) != self.size:
            ids = [str(m.id) for m in self.memories]
            cached = np.empty(self.size, dtype=np.int64)
            cached[sorted(range(self.size), key=ids.__getitem__)] = np.arange(self.size)
            self.__dict__["_id_rank"] = cached
        return cached

    def lexical_scores(self, query: str) -> np.ndarray:
        scores = np.zeros(self.size, dtype=np.float32)
        for doc_id, value in self.bm25.score(query_terms(query)).items():
            scores[doc_id] = value
        return scores

    def concept_scores(self, query: str) -> Optional[np.ndarray]:
        """Weighted share of the query's concepts each memory covers, or
        None when the query evokes no concept. This is the "kind of task"
        signal: a testing question should surface testing memories even
        when they share no words with it. Activity concepts outweigh
        domain concepts (see `core.vocabulary.ACTIVITY_CONCEPTS`); IDF is
        deliberately *not* used, because the activities a developer cares
        about most are exactly the ones with the most memories."""
        concepts = set(query_concepts(query))
        if not concepts:
            return None
        scores = np.zeros(self.size, dtype=np.float32)
        total = 0.0
        for concept in concepts:
            weight = concept_weight(concept.removeprefix(CONCEPT_PREFIX))
            total += weight
            for doc_id, _ in self.bm25.postings.get(concept, ()):
                scores[doc_id] += weight
        return scores / total if total > 0 else None

    def semantic_scores(self, query_vector: Optional[np.ndarray]) -> np.ndarray:
        if query_vector is None or not self.size or not np.any(query_vector):
            return np.zeros(self.size, dtype=np.float32)
        sims = self.vectors @ query_vector.astype(np.float32)
        return np.clip(sims, 0.0, 1.0)

    def similarity(self, i: int, j: int) -> float:
        if self.has_vector[i] and self.has_vector[j]:
            return float(self.vectors[i] @ self.vectors[j])
        return 0.0


class IndexCache:
    """Thread-safe LRU of RetrievalIndex snapshots keyed by
    (visibility, embedding model) and validated by IndexSignature.

    The signature comes from the database on every lookup, so a write
    made by *another* process (a CLI command, a hook, a second API
    worker) is noticed on the next query — no invalidation messages
    needed."""

    def __init__(self, max_entries: int = 64) -> None:
        self._max = max_entries
        self._entries: OrderedDict[str, tuple[IndexSignature, RetrievalIndex]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str, signature: IndexSignature) -> Optional[RetrievalIndex]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] != signature:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry[1]

    def put(self, key: str, signature: IndexSignature, index: RetrievalIndex) -> None:
        with self._lock:
            self._entries[key] = (signature, index)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# ----------------------------------------------------------------------
# Persistent snapshot cache
# ----------------------------------------------------------------------
_SNAPSHOT_FORMAT = 1

#: Everything a snapshot may contain. The unpickler refuses any other
#: global, so a tampered cache file cannot instantiate arbitrary objects
#: (defence in depth: the cache lives in the same owner-only directory as
#: the database itself).
_ALLOWED_GLOBALS: frozenset[tuple[str, str]] = frozenset(
    {
        ("mnemosyne.retrieval.index", "RetrievalIndex"),
        ("mnemosyne.retrieval.bm25", "Bm25Index"),
        ("mnemosyne.domain.entities", "Memory"),
        ("mnemosyne.domain.enums", "MemoryType"),
        ("mnemosyne.domain.enums", "MemoryStatus"),
        ("mnemosyne.domain.enums", "MemoryOrigin"),
        ("mnemosyne.repositories.interfaces", "IndexSignature"),
        ("uuid", "UUID"),
        ("datetime", "datetime"),
        ("datetime", "timezone"),
        ("datetime", "timedelta"),
        ("collections", "defaultdict"),
        ("collections", "OrderedDict"),
        ("builtins", "list"),
        ("builtins", "float"),
        ("builtins", "set"),
        ("builtins", "frozenset"),
        ("builtins", "getattr"),
        ("copyreg", "_reconstructor"),
        ("builtins", "object"),
        ("numpy", "ndarray"),
        ("numpy", "dtype"),
        ("numpy.core.multiarray", "_reconstruct"),
        ("numpy._core.multiarray", "_reconstruct"),
        ("numpy.core.multiarray", "scalar"),
        ("numpy._core.multiarray", "scalar"),
        ("numpy.core.numeric", "_frombuffer"),
        ("numpy._core.numeric", "_frombuffer"),
    }
)


class _SnapshotUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if (module, name) not in _ALLOWED_GLOBALS:
            raise pickle.UnpicklingError(f"forbidden global in index snapshot: {module}.{name}")
        return super().find_class(module, name)


class DiskIndexCache:
    """Persists RetrievalIndex snapshots so that short-lived processes
    (editor hooks run once per prompt) skip the rebuild. Entries are
    validated against the same database IndexSignature as the in-memory
    cache; a stale or unreadable file is simply rebuilt."""

    def __init__(self, directory: Path, *, namespace: str) -> None:
        self._dir = directory
        self._namespace = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:12]

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(f"{self._namespace}|{key}".encode()).hexdigest()[:24]
        return self._dir / f"index-{digest}.pkl"

    def get(self, key: str, signature: IndexSignature) -> Optional[RetrievalIndex]:
        path = self._path(key)
        try:
            with path.open("rb") as handle:
                payload = _SnapshotUnpickler(handle).load()
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("index_snapshot_unreadable", extra={"path": str(path)}, exc_info=True)
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("format") != _SNAPSHOT_FORMAT
            or payload.get("key") != key
            or payload.get("signature") != signature
        ):
            return None
        index = payload.get("index")
        return index if isinstance(index, RetrievalIndex) else None

    def put(self, key: str, signature: IndexSignature, index: RetrievalIndex) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self._path(key)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            with tmp.open("wb") as handle:
                pickle.dump(
                    {"format": _SNAPSHOT_FORMAT, "key": key, "signature": signature, "index": index},
                    handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            os.chmod(tmp, 0o600)
            tmp.replace(path)  # atomic: readers never see a partial file
        except Exception:
            logger.warning("index_snapshot_write_failed", exc_info=True)
