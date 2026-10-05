"""
SQLAlchemy implementations of the supporting repository ports:
evidence, embeddings, identity (users / teams / API keys) and audit.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Sequence, cast
from uuid import UUID

from sqlalchemy import Table, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from mnemosyne.domain.entities import ApiKey, AuditEvent, Evidence, Team, TeamMembership, User
from mnemosyne.domain.exceptions import ConflictError, RepositoryError
from mnemosyne.repositories.interfaces import (
    AuditRepository,
    EmbeddingRepository,
    EvidenceRepository,
    IdentityRepository,
    StoredEmbedding,
)
from mnemosyne.repositories.sqlite.mappers import (
    affected_rows,
    api_key_to_domain,
    audit_to_domain,
    evidence_to_domain,
    membership_to_domain,
    team_to_domain,
    user_to_domain,
)
from mnemosyne.repositories.sqlite.orm_models import (
    ApiKeyORM,
    AuditEventORM,
    MemoryEmbeddingORM,
    MemoryEvidenceORM,
    TeamMemberORM,
    TeamORM,
    UserORM,
)
from mnemosyne.utils.time import utcnow

_IN_CHUNK = 500


class SqlEvidenceRepository(EvidenceRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, evidence: Evidence) -> bool:
        if self.existing_refs(evidence.memory_id, [evidence.source_ref]):
            return False
        self._session.add(
            MemoryEvidenceORM(
                id=str(evidence.id),
                memory_id=str(evidence.memory_id),
                kind=evidence.kind.value,
                source_ref=evidence.source_ref[:255],
                excerpt=evidence.excerpt,
                weight=evidence.weight,
                observed_at=evidence.observed_at,
            )
        )
        try:
            self._session.flush()
        except IntegrityError as exc:  # pragma: no cover - guarded by the pre-check above
            raise ConflictError("duplicate evidence") from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to persist evidence") from exc
        return True

    def list_for_memory(self, memory_id: UUID, *, limit: int = 100) -> list[Evidence]:
        rows = self._session.execute(
            select(MemoryEvidenceORM)
            .where(MemoryEvidenceORM.memory_id == str(memory_id))
            .order_by(MemoryEvidenceORM.observed_at.desc(), MemoryEvidenceORM.id)
            .limit(limit)
        ).scalars()
        return [evidence_to_domain(row) for row in rows]

    def existing_refs(self, memory_id: UUID, refs: Iterable[str]) -> set[str]:
        wanted = [r[:255] for r in refs]
        if not wanted:
            return set()
        rows = self._session.execute(
            select(MemoryEvidenceORM.source_ref).where(
                MemoryEvidenceORM.memory_id == str(memory_id), MemoryEvidenceORM.source_ref.in_(wanted)
            )
        ).scalars()
        return set(rows)

    def trim(self, memory_id: UUID, keep: int) -> int:
        ids = list(
            self._session.execute(
                select(MemoryEvidenceORM.id)
                .where(MemoryEvidenceORM.memory_id == str(memory_id))
                .order_by(MemoryEvidenceORM.observed_at.desc(), MemoryEvidenceORM.id)
                .offset(keep)
            ).scalars()
        )
        if ids:
            self._session.execute(delete(MemoryEvidenceORM).where(MemoryEvidenceORM.id.in_(ids)))
        return len(ids)

    def count_for_memory(self, memory_id: UUID) -> int:
        return int(
            self._session.execute(
                select(func.count()).where(MemoryEvidenceORM.memory_id == str(memory_id))
            ).scalar_one()
        )


class SqlEmbeddingRepository(EmbeddingRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, embedding: StoredEmbedding) -> None:
        orm = self._session.get(MemoryEmbeddingORM, str(embedding.memory_id))
        now = utcnow()
        if orm is None:
            self._session.add(
                MemoryEmbeddingORM(
                    memory_id=str(embedding.memory_id),
                    model=embedding.model,
                    dim=embedding.dim,
                    vector=embedding.vector,
                    text_hash=embedding.text_hash,
                    updated_at=now,
                )
            )
        else:
            orm.model = embedding.model
            orm.dim = embedding.dim
            orm.vector = embedding.vector
            orm.text_hash = embedding.text_hash
            orm.updated_at = now
        try:
            self._session.flush()
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to persist embedding") from exc

    def upsert_many(self, embeddings: Sequence[StoredEmbedding]) -> None:
        """Delete-then-insert in chunks: portable across SQLite and
        PostgreSQL (no dialect-specific ON CONFLICT), and two statements per
        chunk instead of a lookup and flush per row."""
        if not embeddings:
            return
        now = utcnow()
        table = cast(Table, MemoryEmbeddingORM.__table__)  # declarative models always map a Table
        try:
            for start in range(0, len(embeddings), _IN_CHUNK):
                chunk = embeddings[start : start + _IN_CHUNK]
                ids = [str(e.memory_id) for e in chunk]
                self._session.execute(delete(table).where(table.c.memory_id.in_(ids)))
                self._session.execute(
                    insert(table),
                    [
                        {
                            "memory_id": str(e.memory_id),
                            "model": e.model,
                            "dim": e.dim,
                            "vector": e.vector,
                            "text_hash": e.text_hash,
                            "updated_at": now,
                        }
                        for e in chunk
                    ],
                )
            # Rows were written behind the ORM's back; drop any stale
            # identity-map copies.
            for orm in [o for o in self._session.identity_map.values() if isinstance(o, MemoryEmbeddingORM)]:
                self._session.expunge(orm)
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to persist embeddings") from exc

    def get_many(self, memory_ids: Sequence[UUID], *, model: str) -> dict[UUID, StoredEmbedding]:
        out: dict[UUID, StoredEmbedding] = {}
        ids = [str(i) for i in memory_ids]
        table = cast(Table, MemoryEmbeddingORM.__table__)  # declarative models always map a Table
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start : start + _IN_CHUNK]
            rows = self._session.execute(
                select(table.c.memory_id, table.c.model, table.c.dim, table.c.vector, table.c.text_hash).where(
                    table.c.memory_id.in_(chunk), table.c.model == model
                )
            )
            for memory_id, model_, dim, vector, text_hash in rows:
                key = UUID(memory_id)
                out[key] = StoredEmbedding(key, model_, dim, bytes(vector), text_hash)
        return out

    def text_hashes(self, *, model: str) -> dict[UUID, str]:
        rows = self._session.execute(
            select(MemoryEmbeddingORM.memory_id, MemoryEmbeddingORM.text_hash).where(MemoryEmbeddingORM.model == model)
        )
        return {UUID(memory_id): text_hash for memory_id, text_hash in rows}

    def delete_other_models(self, *, keep_model: str) -> int:
        result = self._session.execute(delete(MemoryEmbeddingORM).where(MemoryEmbeddingORM.model != keep_model))
        return affected_rows(result)


class SqlIdentityRepository(IdentityRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def _flush(self, what: str) -> None:
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise ConflictError(f"{what} already exists") from exc
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to persist {what}") from exc

    # --- users ---------------------------------------------------------
    def add_user(self, user: User) -> None:
        self._session.add(
            UserORM(
                id=user.id,
                handle=user.handle,
                display_name=user.display_name,
                is_admin=user.is_admin,
                disabled=user.disabled,
                created_at=user.created_at,
            )
        )
        self._flush(f"user {user.handle!r}")

    def get_user(self, user_id: str) -> Optional[User]:
        orm = self._session.get(UserORM, user_id)
        return user_to_domain(orm) if orm else None

    def get_user_by_handle(self, handle: str) -> Optional[User]:
        orm = self._session.execute(select(UserORM).where(UserORM.handle == handle)).scalar_one_or_none()
        return user_to_domain(orm) if orm else None

    def list_users(self) -> list[User]:
        return [user_to_domain(o) for o in self._session.execute(select(UserORM).order_by(UserORM.handle)).scalars()]

    def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        self._session.execute(update(UserORM).where(UserORM.id == user_id).values(disabled=disabled))

    # --- teams ---------------------------------------------------------
    def add_team(self, team: Team) -> None:
        self._session.add(TeamORM(id=team.id, name=team.name, created_at=team.created_at))
        self._flush(f"team {team.name!r}")

    def get_team(self, team_id: str) -> Optional[Team]:
        orm = self._session.get(TeamORM, team_id)
        return team_to_domain(orm) if orm else None

    def get_team_by_name(self, name: str) -> Optional[Team]:
        orm = self._session.execute(select(TeamORM).where(TeamORM.name == name)).scalar_one_or_none()
        return team_to_domain(orm) if orm else None

    def list_teams(self) -> list[Team]:
        return [team_to_domain(o) for o in self._session.execute(select(TeamORM).order_by(TeamORM.name)).scalars()]

    def upsert_membership(self, membership: TeamMembership) -> None:
        orm = self._session.get(TeamMemberORM, (membership.team_id, membership.user_id))
        if orm is None:
            self._session.add(
                TeamMemberORM(
                    team_id=membership.team_id,
                    user_id=membership.user_id,
                    role=membership.role.value,
                    joined_at=membership.joined_at,
                )
            )
        else:
            orm.role = membership.role.value
        self._flush("team membership")

    def remove_membership(self, team_id: str, user_id: str) -> bool:
        result = self._session.execute(
            delete(TeamMemberORM).where(TeamMemberORM.team_id == team_id, TeamMemberORM.user_id == user_id)
        )
        return affected_rows(result) > 0

    def memberships_for_user(self, user_id: str) -> list[TeamMembership]:
        rows = self._session.execute(select(TeamMemberORM).where(TeamMemberORM.user_id == user_id)).scalars()
        return [membership_to_domain(o) for o in rows]

    def members_of_team(self, team_id: str) -> list[TeamMembership]:
        rows = self._session.execute(select(TeamMemberORM).where(TeamMemberORM.team_id == team_id)).scalars()
        return [membership_to_domain(o) for o in rows]

    # --- API keys ------------------------------------------------------
    def add_api_key(self, key: ApiKey) -> None:
        self._session.add(
            ApiKeyORM(
                id=key.id,
                user_id=key.user_id,
                name=key.name,
                prefix=key.prefix,
                key_hash=key.key_hash,
                scopes=list(key.scopes),
                created_at=key.created_at,
                last_used_at=key.last_used_at,
                expires_at=key.expires_at,
                revoked_at=key.revoked_at,
            )
        )
        self._flush("API key")

    def get_api_key_by_prefix(self, prefix: str) -> Optional[ApiKey]:
        orm = self._session.execute(select(ApiKeyORM).where(ApiKeyORM.prefix == prefix)).scalar_one_or_none()
        return api_key_to_domain(orm) if orm else None

    def get_api_key(self, key_id: str) -> Optional[ApiKey]:
        orm = self._session.get(ApiKeyORM, key_id)
        return api_key_to_domain(orm) if orm else None

    def list_api_keys(self, user_id: str) -> list[ApiKey]:
        rows = self._session.execute(
            select(ApiKeyORM).where(ApiKeyORM.user_id == user_id).order_by(ApiKeyORM.created_at)
        ).scalars()
        return [api_key_to_domain(o) for o in rows]

    def touch_api_key(self, key_id: str, when: datetime) -> None:
        self._session.execute(update(ApiKeyORM).where(ApiKeyORM.id == key_id).values(last_used_at=when))

    def revoke_api_key(self, key_id: str, when: datetime) -> bool:
        result = self._session.execute(
            update(ApiKeyORM).where(ApiKeyORM.id == key_id, ApiKeyORM.revoked_at.is_(None)).values(revoked_at=when)
        )
        return affected_rows(result) > 0


class SqlAuditRepository(AuditRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, event: AuditEvent) -> None:
        self._session.add(
            AuditEventORM(
                id=str(event.id),
                occurred_at=event.occurred_at,
                actor_id=event.actor_id[:64],
                action=event.action[:64],
                target_type=event.target_type[:32],
                target_id=(event.target_id or None) and event.target_id[:64],
                request_id=(event.request_id or None) and event.request_id[:64],
                details=dict(event.details),
            )
        )
        try:
            self._session.flush()
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to write audit event") from exc

    def list(
        self,
        *,
        actor_id: Optional[str] = None,
        action_prefix: Optional[str] = None,
        target_id: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        stmt = select(AuditEventORM)
        if actor_id is not None:
            stmt = stmt.where(AuditEventORM.actor_id == actor_id)
        if action_prefix:
            stmt = stmt.where(AuditEventORM.action.startswith(action_prefix))
        if target_id is not None:
            stmt = stmt.where(AuditEventORM.target_id == target_id)
        if since is not None:
            stmt = stmt.where(AuditEventORM.occurred_at >= since)
        stmt = stmt.order_by(AuditEventORM.occurred_at.desc(), AuditEventORM.id).limit(limit)
        return [audit_to_domain(o) for o in self._session.execute(stmt).scalars()]
