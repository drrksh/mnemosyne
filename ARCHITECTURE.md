# Architecture

## Layers

```
┌──────────────────────────────────────────────────────────────────┐
│  API Layer            FastAPI routers, Pydantic schemas           │
│  src/mnemosyne/api/    HTTP ↔ domain translation, error mapping    │
└───────────────────────────────┬────────────────────────────────────┘
                                  │  calls
┌───────────────────────────────▼────────────────────────────────────┐
│  Service Layer                 MemoryService                        │
│  src/mnemosyne/services/        One UnitOfWork per use-case,          │
│                                  commits exactly once, logs            │
└───────────────────────────────┬────────────────────────────────────┘
                       delegates rules to │  │  calls through UnitOfWork
              ┌────────────────────────────┘  └───────────────────────┐
┌─────────────▼───────────────────┐               ┌────────────────────▼───────────────┐
│  Core / "Memory Engine"           │               │  Repository Layer                    │
│  src/mnemosyne/core/               │               │  src/mnemosyne/repositories/           │
│  MemoryEngine — validation,         │               │  interfaces.py: the port (ABC)         │
│  version-snapshot construction,       │               │  sqlite/: the only adapter today        │
│  status-transition rules. Zero I/O.     │               │  ORM models, mappers, SQL             │
└─────────────┬───────────────────┘               └────────────────────┬───────────────┘
               │  operates on                                              │  persists via
┌─────────────▼─────────────────────────────────────────────────────────▼───────────────┐
│  Domain Layer                                                                              │
│  src/mnemosyne/domain/   Memory, MemoryVersion, enums, exceptions, DTOs                      │
│  No SQLAlchemy. No FastAPI. No Pydantic. This is what a "memory" IS.                          │
└───────────────────────────────────────────────────────────────────────────────────────────┘
                                                │  reads/writes via SQLAlchemy
┌───────────────────────────────────────────────▼───────────────────────────────────────────┐
│  Storage Layer            src/mnemosyne/db/   Engine/session construction, Unit of Work        │
│                             SQLite today — see "Swapping the database" below                    │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

Every arrow points inward, toward the domain layer — this is the
Dependency Inversion Principle applied deliberately: **the domain layer
depends on nothing else in the project.** The API and repository layers
both depend on domain types; they never depend on each other's
internals. `core/memory_engine.py` has zero imports from `sqlalchemy`,
`fastapi`, or `pydantic` — verify this yourself with
`grep -rn "^from\|^import" src/mnemosyne/core/` if you like; it is a
genuine, checkable property of the codebase, not just a diagram claim.

This differs slightly from the six-layer suggestion in the original
brief (API → Service → Memory Engine → Repository → Storage → Database)
in one respect: **Core and Repository are siblings, not a strict chain**
— both depend on Domain, but neither depends on the other. The Service
layer is what wires them together per use-case. This is a meaningful
improvement over a strict linear chain: it means the validation rules
in `MemoryEngine` can be fully unit-tested (as they are — see
`tests/unit/test_memory_engine.py`) without a database anywhere in
sight, and a future alternate repository implementation never needs to
know anything about validation rules.

## Data model

```
┌────────────────────┐        ┌──────────────────────┐
│      memories        │        │    memory_versions      │
├────────────────────┤        ├──────────────────────┤
│ id            PK      │◄───┐   │ id                PK      │
│ title                  │    │   │ memory_id     FK ─┘        │
│ summary                │    │   │ version_number             │
│ content                │    │   │ title, summary, content…    │
│ memory_type            │    │   │ changed_fields   (JSON)      │
│ confidence_score       │    │   │ change_reason                │
│ importance_score       │    │   │ recorded_at                   │
│ status                 │    │   └──────────────────────┘
│ version    (optimistic-│    │   UNIQUE(memory_id, version_number)
│  concurrency token)     │    │   Only ever holds SUPERSEDED states —
│ source                  │    │   the live `memories` row IS the
│ metadata      (JSON)    │    │   current version. See "Versioning"
│ notes                   │    │   below for why.
│ created_at / updated_at │    │
│ access_count            │    │        ┌───────────┐
│ last_accessed_at        │    │        │    tags      │
│ deleted_at              │    │        ├───────────┤
└──────────┬──────────┘    │        │ id     PK    │
            │                    │        │ name  UNIQUE │
            │  M:N via             │        └─────┬─────┘
            └── memory_tags ───────────────────────┘
                (memory_id, tag_id — both FK ON DELETE CASCADE)
```

Tags are normalized (their own table + join table) rather than stored
as a JSON array on `memories`, specifically so tag search can use a
real B-tree index instead of scanning and parsing JSON per row — see
DECISIONS.md.

## Versioning & concurrency

**The rule:** the `memories` table always holds the *current* state.
`memory_versions` holds only *superseded* states, one row per prior
version, oldest first. An update:

1. Loads the current row.
2. Builds a `MemoryVersion` snapshot of *that current state* (before
   any change is applied) — this becomes the historical record.
3. Applies the new field values to the live row.
4. Both the version-insert and the row-update happen inside the same
   database transaction (see "Transactions" below) — either both
   happen or neither does.

Asking "what did this look like 3 months ago" means walking
`memory_versions` for the most recent snapshot at-or-before that date;
asking for the *current* state just means reading `memories` directly.
This avoids ever storing the same state twice (the live row is never
duplicated into the version table until it's actually superseded).

**Concurrency** is enforced two independent ways:

- **SQLAlchemy's built-in optimistic concurrency** (`version_id_col`
  on `MemoryORM`). Every `UPDATE` includes `WHERE version = <the value
  this session loaded>`, and SQLAlchemy increments it atomically. If
  zero rows match — because someone else's write already moved the
  version forward — SQLAlchemy raises `StaleDataError`, which the
  repository translates into `ConflictError`. This protects against
  races within the lifetime of a single request, with no application
  code required to get it right.
- **Explicit client-supplied `expected_version`.** A PATCH request can
  include `expected_version: 3` to mean "only apply this if the memory
  is still at version 3" — a business-level "I read this, nothing
  should have changed since" contract, checked explicitly in
  `MemoryService` before the engine even runs.

## Transactions

Every write-use-case in `MemoryService` opens exactly one
`UnitOfWork`, does its repository calls, and calls `.commit()` once.
`UnitOfWork.__exit__` rolls back automatically if the block exits via
an exception (validation failure, conflict, anything) — the caller
never has to remember to clean up, which is what `tests/failure/` and
`tests/integration/test_memory_service.py::test_bulk_update_is_atomic`
verify directly: an invalid item partway through a bulk update leaves
*zero* changes committed, not a partial batch.

## Request lifecycle (a single `PATCH /api/v1/memories/{id}`)

1. **`RequestContextMiddleware`** (api/app.py) assigns a correlation
   id, logs `request_started`, starts a timer.
2. **FastAPI/Pydantic** parses and validates the JSON body against
   `MemoryUpdateRequest` — malformed JSON or an out-of-range field
   never reaches application code; it's a 422 before `core` or
   `services` are involved at all.
3. **The router** (`api/routers/memories.py`) converts the validated
   Pydantic model to a `MemoryUpdateData` DTO and calls
   `MemoryService.update_memory(...)`. No business logic here.
4. **`MemoryService`** opens a `UnitOfWork`, fetches the current
   `Memory`, checks `expected_version` if supplied, and hands off to
   `MemoryEngine.apply_update(...)`.
5. **`MemoryEngine`** validates every changed field (in isolation from
   any database), computes the sparse set of *actually different*
   values, and builds the pre-update version snapshot. It returns an
   `UpdatePlan`; if nothing actually changed, `is_noop=True` and the
   service skips touching the database at all.
6. **`MemoryService`** calls `uow.memories.add_version(...)` then
   `uow.memories.update(...)`, then commits.
7. **`SqliteMemoryRepository`** executes the SQL, letting SQLAlchemy's
   `version_id_col` enforce the concurrency check, and maps the
   updated ORM row back to a domain `Memory`.
8. **The router** maps the returned `Memory` to a `MemoryResponse` and
   FastAPI serializes it.
9. **`RequestContextMiddleware`** logs `request_finished` with the
   status code and duration, and stamps `X-Request-ID` on the
   response.

Any `MnemosyneError` raised anywhere in steps 4–7 is caught once, in
`api/error_handlers.py`, and mapped to the right HTTP status — nothing
in between needs its own try/except for HTTP concerns.

## Swapping the database (SQLite → PostgreSQL → distributed)

Nothing above the Storage layer knows SQLite exists. Concretely:

- `MemoryEngine`, every domain type, and `MemoryService` never import
  `sqlalchemy` at all.
- `MemoryRepository` (the abstract port) is storage-agnostic by
  construction — it's an `ABC` with no SQL in it.
- `UnitOfWork` takes its repository implementation as a constructor
  parameter (`repository_factory`, defaulting to
  `SqliteMemoryRepository`) rather than hardcoding it — a
  `PostgresMemoryRepository` implementing the same `MemoryRepository`
  contract could be swapped in by changing one default argument.
- The only SQLite-specific code in the whole project is
  `db/session.py` (WAL pragma, busy-timeout, `StaticPool` for
  `:memory:`) and the `render_as_batch=True` flag in
  `migrations/env.py` (needed because SQLite can't `ALTER TABLE` most
  things in place — this flag is a documented no-op on backends that
  support `ALTER` natively, so the same migration file works
  unmodified against PostgreSQL).

Moving to PostgreSQL is: change `DATABASE_URL`, drop the SQLite-only
pragmas from `session.py`, run `alembic upgrade head`. No change to
`domain/`, `core/`, `services/`, or `api/`.

## Extension points for future phases

| Future capability (see ROADMAP.md) | Where it attaches |
|--------------------------------------|----------------------|
| Vector / semantic search               | New method or new implementation behind `MemoryRepository.search()` — `SearchCriteria`/`SearchResult` are already the stable contract |
| Embeddings                              | A new field on `Memory`/`MemoryORM` plus a new repository method; `MemoryEngine` would gain a validation rule, nothing upstream changes |
| LLM integration / context compilation    | An entirely new layer *above* `MemoryService` — it would call the existing service the same way the API layer does today |
| Knowledge graph / relationships           | `MemoryType.RELATIONSHIP` already exists as a vocabulary entry; a graph store would be a second repository alongside `MemoryRepository`, not a replacement |
| Caching layer                             | Wraps `MemoryRepository` (decorator implementing the same port) — services/API never know it's there |
| Multi-user / permissions                    | A `user_id` column + a scoping filter in `SearchCriteria`/repository queries; `MemoryEngine` gains an authorization check |
| Encryption                                | A repository-level concern (encrypt on write, decrypt on read) — implementable as another `MemoryRepository` decorator |
| Cloud / distributed storage                 | A new `MemoryRepository` implementation; `UnitOfWork`'s `repository_factory` parameter exists specifically for this |

## Observability

Every log line is structured JSON (or human-readable in `console`
mode) carrying a request-correlation id, emitted through the standard
library's `logging` module — see `logging_config.py`. This is a
deliberate choice over a third-party structured-logging dependency (see
DECISIONS.md): fewer things that can break across a long timeline, and
every call site is just `logging.getLogger("mnemosyne.<component>")`,
which is trivially redirectable to a real observability stack later.
