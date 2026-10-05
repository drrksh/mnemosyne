"""
Composition root.

`Mnemosyne` wires configuration, the database, the domain engine, the
retrieval stack and every service together. The REST API, the CLI, the
MCP server and the editor hooks all construct exactly this object, so
they cannot drift apart in behavior: a memory learned through a hook is
recalled identically through MCP, the API, or the command line.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from sqlalchemy import Engine

from mnemosyne.config import Settings, get_settings
from mnemosyne.core.memory_engine import EngineLimits, MemoryEngine
from mnemosyne.db import migrate
from mnemosyne.db.base import Base
from mnemosyne.db.session import create_engine_from_settings, create_session_factory, is_in_memory_sqlite
from mnemosyne.db.unit_of_work import UnitOfWork
from mnemosyne.domain.principal import Permission, Principal
from mnemosyne.repositories.sqlite import orm_models  # noqa: F401  (registers models on Base.metadata)
from mnemosyne.retrieval.embeddings import Embedder, build_embedder
from mnemosyne.retrieval.index import DiskIndexCache, IndexCache
from mnemosyne.retrieval.indexer import MemoryIndexer
from mnemosyne.retrieval.ranker import HybridRanker, RankingWeights
from mnemosyne.services.identity_service import IdentityService
from mnemosyne.services.memory_service import MemoryService
from mnemosyne.services.retrieval_service import RetrievalService

logger = logging.getLogger("mnemosyne.container")

SchemaMode = Literal["migrate", "create", "none"]


class Mnemosyne:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        db_engine: Optional[Engine] = None,
        schema: Optional[SchemaMode] = None,
        embedder: Optional[Embedder] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db_engine = db_engine or create_engine_from_settings(self.settings)
        self._prepare_schema(schema or ("migrate" if self.settings.auto_migrate else "none"))
        self.session_factory = create_session_factory(self.db_engine)

        self.memory_engine = MemoryEngine(
            EngineLimits(
                max_title_length=self.settings.max_title_length,
                max_summary_length=self.settings.max_summary_length,
                max_tags_per_memory=self.settings.max_tags_per_memory,
                max_tag_length=self.settings.max_tag_length,
                max_metadata_bytes=self.settings.max_metadata_bytes,
                max_content_length=self.settings.max_content_length,
            )
        )
        self.embedder = embedder or build_embedder(self.settings)
        self.indexer = MemoryIndexer(self.embedder)
        self.index_cache = IndexCache()
        self.ranker = HybridRanker(RankingWeights.from_settings(self.settings))

        self.identity = IdentityService(self.uow_factory)
        self.default_principal = Principal(user_id=self.settings.local_user_id, permissions=Permission.ALL)

        self.memories = MemoryService(
            self.uow_factory, self.memory_engine, indexer=self.indexer, default_principal=self.default_principal
        )
        disk_cache = None
        if not is_in_memory_sqlite(self.settings.database_url):
            disk_cache = DiskIndexCache(self.settings.cache_dir, namespace=self.settings.database_url)
        self.retrieval = RetrievalService(
            self.uow_factory,
            self.indexer,
            self.ranker,
            self.index_cache,
            default_principal=self.default_principal,
            disk_cache=disk_cache,
        )

    # ------------------------------------------------------------------
    def uow_factory(self) -> UnitOfWork:
        return UnitOfWork(self.session_factory)

    def local_principal(self) -> Principal:
        """The local user, including any team memberships. Ensures the user
        row exists (single-user mode bootstraps itself)."""
        self.identity.ensure_user(self.settings.local_user_id, is_admin=True)
        return self.identity.principal_for(self.settings.local_user_id, Permission.ALL)

    def _prepare_schema(self, mode: SchemaMode) -> None:
        if mode == "migrate":
            if not migrate.is_up_to_date(self.db_engine):
                logger.info("applying_pending_migrations")
                migrate.upgrade(self.db_engine)
        elif mode == "create":
            Base.metadata.create_all(self.db_engine)

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.db_engine.dispose()

    def __enter__(self) -> Mnemosyne:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
