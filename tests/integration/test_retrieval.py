"""Retrieval behaviour: ranking signals, scope handling, caching, and
graceful degradation."""
from __future__ import annotations

import os
import pickle

import numpy as np
import pytest

from mnemosyne.config import Settings
from mnemosyne.container import Mnemosyne
from mnemosyne.domain.dto import MemoryUpdateData, NewMemoryData
from mnemosyne.domain.enums import MemoryType
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.repositories.interfaces import IndexSignature
from mnemosyne.retrieval.embeddings import Embedder, HashingEmbedder
from mnemosyne.retrieval.index import DiskIndexCache
from mnemosyne.services.retrieval_service import RecallRequest


def _create(container, title, content="", **kw):
    return container.memories.create_memory(NewMemoryData(title=title, content=content, **kw))


def test_recall_ranks_relevant_memory_first(container):
    _create(container, "Tests use pytest", "pytest with fixtures in conftest.py", scope="lang:python")
    _create(container, "Deploy with Argo CD", "merging to main deploys to staging")
    result = container.retrieval.recall(RecallRequest(query="how do I write unit tests"))
    assert result.items[0].memory.title == "Tests use pytest"


def test_recall_excludes_other_repositories_and_languages(container):
    _create(container, "Use pnpm for installs", scope="repo:github.com/acme/web")
    _create(container, "Use uv for installs", scope="repo:github.com/acme/api")
    _create(container, "Go installs via go install", scope="lang:go")
    ctx = WorkingContext.create(repo_id="github.com/acme/api", languages=["python"])
    titles = [s.memory.title for s in container.retrieval.recall(RecallRequest(query="installs", context=ctx)).items]
    assert titles == ["Use uv for installs"]


def test_slot_resolution_keeps_most_specific_scope(container):
    _create(container, "Prefers pytest", scope="lang:python", key="python.test_framework", value="pytest")
    _create(container, "This repo uses unittest", scope="repo:x/legacy", key="python.test_framework", value="unittest")
    ctx = WorkingContext.create(repo_id="x/legacy", languages=["python"])
    result = container.retrieval.recall(RecallRequest(query="test framework", context=ctx))
    assert [s.memory.value for s in result.items] == ["unittest"]
    assert [s.memory.value for s in result.overridden] == ["pytest"]


def test_score_breakdown_is_reported(container):
    _create(container, "Structured logging", "use logging.getLogger with extra fields")
    item = container.retrieval.recall(RecallRequest(query="logging")).items[0]
    breakdown = item.breakdown.as_dict()
    assert {"semantic", "lexical", "concept", "relevance", "scope", "total"} <= set(breakdown)
    assert breakdown["lexical"] > 0
    assert item.score == pytest.approx(item.breakdown.total)


def test_irrelevant_queries_return_nothing(container):
    _create(container, "Structured logging", "use logging.getLogger with extra fields")
    assert container.retrieval.recall(RecallRequest(query="kubernetes helm chart values")).items == []


def test_filters_by_type_and_min_confidence(container):
    _create(container, "Logging rule", memory_type=MemoryType.RULE, confidence_score=0.9)
    _create(container, "Logging fact", memory_type=MemoryType.FACT, confidence_score=0.4)
    result = container.retrieval.recall(
        RecallRequest(query="logging", memory_types=[MemoryType.RULE, MemoryType.FACT], min_confidence=0.5)
    )
    assert [s.memory.title for s in result.items] == ["Logging rule"]


def test_index_cache_reflects_writes(container):
    memory = _create(container, "Old wording about caching")
    assert container.retrieval.recall(RecallRequest(query="caching")).items
    container.memories.update_memory(memory.id, MemoryUpdateData(title="Something about queues"))
    assert container.retrieval.recall(RecallRequest(query="caching")).items == []
    assert container.retrieval.recall(RecallRequest(query="queues")).items


def test_mmr_diversifies_near_duplicates(container):
    for i in range(4):
        _create(container, f"Use pytest fixtures variant {i}", "pytest fixtures conftest")
    _create(container, "Use parametrize for table tests", "pytest parametrize")
    result = container.retrieval.recall(RecallRequest(query="pytest", limit=3, diversify=True))
    titles = [s.memory.title for s in result.items]
    assert "Use parametrize for table tests" in titles


class _FailingEmbedder(Embedder):
    model_id = "failing"
    dim = 8

    def embed_documents(self, texts):
        raise RuntimeError("embedding service down")


def test_embedder_outage_degrades_to_lexical(test_settings):
    with Mnemosyne(test_settings, schema="create", embedder=_FailingEmbedder()) as app:
        app.memories.create_memory(NewMemoryData(title="Structured logging"))  # write still succeeds
        result = app.retrieval.recall(RecallRequest(query="logging"))
        assert [s.memory.title for s in result.items] == ["Structured logging"]
        assert result.semantic_available is False


def test_hashing_embedder_is_deterministic_and_normalized():
    a, b = HashingEmbedder(dim=128), HashingEmbedder(dim=128)
    va, vb = a.embed_one("pytest fixtures"), b.embed_one("pytest fixtures")
    assert np.array_equal(va, vb)
    assert np.linalg.norm(va) == pytest.approx(1.0, abs=1e-5)
    assert float(va @ a.embed_one("pytest fixture")) > float(va @ a.embed_one("kubernetes deployment"))


# ----------------------------------------------------------------------
# Persistent snapshot cache
# ----------------------------------------------------------------------
@pytest.fixture
def file_container(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'm.db'}", home=tmp_path, log_level="WARNING")
    with Mnemosyne(settings) as app:
        yield app, settings


def test_snapshot_is_reused_by_a_new_process(file_container):
    app, settings = file_container
    app.memories.create_memory(NewMemoryData(title="Structured logging"))
    app.retrieval.load_index()
    with Mnemosyne(settings) as fresh:
        assert fresh.retrieval.disk_cache is not None
        signature = None
        with fresh.uow_factory() as uow:
            signature = uow.memories.index_signature(fresh.default_principal.visibility)
            uow.commit()
        key = f"{fresh.default_principal.visibility.cache_key}|{fresh.indexer.model_id}"
        assert fresh.retrieval.disk_cache.get(key, signature) is not None
        assert fresh.retrieval.recall(RecallRequest(query="logging")).items


def test_snapshot_with_stale_signature_is_ignored(tmp_path, file_container):
    app, _ = file_container
    app.memories.create_memory(NewMemoryData(title="Structured logging"))
    index = app.retrieval.load_index()
    cache = DiskIndexCache(tmp_path / "c", namespace="x")
    sig = IndexSignature(count=1, max_updated_at=None, version_sum=1)
    cache.put("k", sig, index)
    assert cache.get("k", sig) is not None
    assert cache.get("k", IndexSignature(count=2, max_updated_at=None, version_sum=2)) is None
    assert cache.get("other", sig) is None


class _Exploit:
    def __reduce__(self):
        return (os.system, ("echo pwned",))


def test_snapshot_loader_refuses_arbitrary_objects(tmp_path):
    cache = DiskIndexCache(tmp_path, namespace="x")
    path = cache._path("k")
    path.write_bytes(pickle.dumps({"format": 1, "key": "k", "index": _Exploit()}))
    assert cache.get("k", IndexSignature(count=0, max_updated_at=None, version_sum=0)) is None


def test_corrupted_snapshot_is_ignored(tmp_path):
    cache = DiskIndexCache(tmp_path, namespace="x")
    cache._path("k").write_bytes(b"not a pickle")
    assert cache.get("k", IndexSignature(count=0, max_updated_at=None, version_sum=0)) is None


def test_equal_scores_order_deterministically(container):
    """Ties must not be broken by load order or by seconds of recency
    jitter: the same memory must produce the same context text, both for
    users and for assistants' prompt caches."""
    from datetime import timedelta

    from mnemosyne.domain.enums import MemoryType
    from mnemosyne.utils.time import utcnow

    created = container.memories.create_memories_batch([
        NewMemoryData(title=f"Convention number {i}", memory_type=MemoryType.CONVENTION, key=f"style.rule_{i}",
                      value="yes", importance_score=0.6, confidence_score=0.9)
        for i in range(12)
    ])
    index = container.retrieval.load_index()
    ranker = container.ranker
    now = utcnow()
    orders = [
        [s.memory.id for s in ranker.rank(index, query=None, query_vector=None, context=WorkingContext(),
                                          now=now + timedelta(seconds=offset), require_relevance=False)]
        for offset in (0, 7, 45)
    ]
    assert orders[0] == orders[1] == orders[2]
    assert orders[0] == sorted((m.id for m in created), key=str)  # exact ties fall back to id order

    reversed_index = type(index).build(model_id=index.model_id, memories=list(reversed(index.memories)),
                                       vectors=index.vectors[::-1].copy(), has_vector=index.has_vector[::-1].copy())
    again = [s.memory.id for s in ranker.rank(reversed_index, query=None, query_vector=None,
                                              context=WorkingContext(), now=now, require_relevance=False)]
    assert again == orders[0]
