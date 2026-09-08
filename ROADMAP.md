# Roadmap

## Phase 1 — Local Memory Engine (this repository)

Structured memory storage: CRUD, versioning, search, a REST API, and
the architectural seams every later phase will attach to. Deliberately
excludes embeddings, vector search, LLM integration, context
compilation, and UI — see the README's "What Phase 1 is" section.

**Status: complete.** 114 tests passing, 95% coverage, mypy clean,
real Alembic migrations verified against a live database.

---

## Phase 2 — Semantic Search

**Goal:** find memories by meaning, not just keyword/tag/filter match.

**Attaches at:** `MemoryRepository.search()`. `SearchCriteria` and
`SearchResult` (`domain/dto.py`) are already the stable contract
between callers and the search implementation — Phase 2 either extends
`SqliteMemoryRepository.search()` to also rank by embedding similarity,
or introduces a second repository (e.g. wrapping a vector index) that
implements the same `MemoryRepository` port. Either way, `MemoryService`
and the API layer do not change.

**New pieces likely needed:** an embedding field on `Memory`
(`embedding: list[float] | None`, nullable so existing rows remain
valid), a background or on-write embedding-generation step, and a
choice of vector index (SQLite extension, or a dedicated vector store
behind the same repository port).

---

## Phase 3 — LLM Integration & Context Compilation

**Goal:** the actual "reasoning engine sits on top" layer described in
the original brief — turning a set of retrieved memories into a
context window an LLM can use, and writing new memories back from a
conversation.

**Attaches at:** a new layer *above* `MemoryService`, calling it the
same way `api/routers/memories.py` does today. This phase is where an
LLM first enters the system — Phase 1 has zero LLM dependencies by
design, so this phase can choose any model/provider without
untangling anything below it.

**New pieces likely needed:** a context-window budget/ranking
strategy (which memories matter most for this conversation — likely
consuming `importance_score`, `confidence_score`, and
`access_count`/`last_accessed_at`, all of which already exist on
`Memory` for exactly this purpose), and a write-back path that calls
`MemoryService.create_memory` / `.update_memory` from conversation
content.

---

## Phase 4 — Knowledge Graph / Relationships

**Goal:** explicit relationships between memories, not just
independent records.

**Attaches at:** `MemoryType.RELATIONSHIP` already exists in the
vocabulary (`domain/enums.py`) as a placeholder for this. A real graph
engine would most naturally be a second repository/store living
alongside `MemoryRepository`, referencing memory ids, rather than a
replacement for the relational store — most memories don't need graph
semantics, and forcing everything through a graph model would cost
the simplicity of the current CRUD path for no benefit to the common
case.

---

## Phase 5 — Multi-User & Permissions

**Goal:** more than one person/agent sharing a memory store, with
access control.

**Attaches at:** a `user_id` (or `owner_id`) column on `memories`, a
scoping filter added to `SearchCriteria` and every repository query,
and an authorization check added to `MemoryEngine` (or a new
`AuthorizationService` the API layer consults before calling
`MemoryService`). The Unit-of-Work-per-use-case transaction pattern
does not need to change — it already isolates each use-case cleanly.

---

## Phase 6 — Caching, Distributed Storage, Encryption

These three are grouped because they share the same extension point:
**a decorator implementing `MemoryRepository`** that wraps
`SqliteMemoryRepository` (or its PostgreSQL successor) and is injected
via `UnitOfWork`'s `repository_factory` parameter — see
`db/unit_of_work.py` and DECISIONS.md #2.

- **Caching:** a `CachingMemoryRepository` that checks an in-memory or
  Redis cache before delegating reads to the wrapped repository, and
  invalidates on writes.
- **Distributed storage:** a new repository implementation entirely
  (the SQLite→PostgreSQL migration path documented in ARCHITECTURE.md
  is the template; a distributed store follows the same shape —
  implement the port, nothing upstream changes).
- **Encryption:** an `EncryptingMemoryRepository` that encrypts
  sensitive fields on write and decrypts on read, wrapping the
  underlying storage the same way caching would.

---

## Phase 7 — Team Memory, Cloud Sync, Plugin System

Longer-horizon items explicitly named in the original brief's
Extensibility section. No concrete design work has been done for these
yet — they're listed here as acknowledged future scope, not
pre-architected. What Phase 1 guarantees for them: the domain layer
has no assumptions baked in that would block any of the three (no
single-user assumptions in the entity model beyond what Phase 5
addresses; no local-filesystem assumptions beyond `db/session.py`,
which is already isolated specifically so it can change).

---

## What would need to change in Phase 1 to support all of the above

As little as possible, by design — but for completeness, the honest
list of Phase 1 seams a later phase will actually touch:

- `domain/entities.py` — new nullable fields (embedding vector,
  owner_id) as needed. Additive, not breaking.
- `domain/enums.py` — new `MemoryType` values as needed (already a
  one-line change per DECISIONS.md #3).
- `repositories/interfaces.py` — potentially new methods on the port
  if a capability genuinely can't be expressed through `search()`
  alone (e.g. a dedicated `find_similar()` for embeddings) — additive.
- `db/unit_of_work.py`'s `repository_factory` parameter — this is the
  intended point where Phase 6's decorators attach; no change needed
  to `UnitOfWork` itself.
- Everything in `core/` and `services/` should need *behavioral*
  additions (new use-cases) but not structural rewrites — that was the
  point of keeping them free of infrastructure dependencies.
