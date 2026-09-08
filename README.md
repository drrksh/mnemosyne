# Mnemosyne — Phase 1: Local Memory Engine

Mnemosyne is a **Memory Operating System** that sits between an LLM and
long-term storage, letting an AI accumulate structured experience across
years instead of restarting from zero every conversation.

**This is not a chatbot. This is not RAG. This is not an LLM wrapper.**
The LLM is a reasoning engine that sits *on top of* this system — this
repository contains none of that. It contains the foundation everything
else will be built on: a durable, versioned, queryable store for
structured memories.

## What Phase 1 is

Phase 1 builds exactly one thing: the **Local Memory Engine** — a
production-grade CRUD + versioning + search system for structured
memories, backed by SQLite today and portable to PostgreSQL (or a
distributed store) later without touching business logic.

Explicitly **not** in this phase — see [ROADMAP.md](ROADMAP.md) for
where each of these will attach:

- No embeddings, no vector search
- No LLM integration of any kind
- No context compilation
- No UI

## Quickstart

```bash
# 1. Install (creates an editable install with dev dependencies)
make dev-install

# 2. Apply database migrations (creates ./mnemosyne.db)
make migrate

# 3. Run the API
make run
# -> http://localhost:8420/docs for interactive OpenAPI documentation
```

Configuration is entirely environment-driven — copy `.env.example` to
`.env` and adjust as needed. Every setting has a sensible default; see
[`src/mnemosyne/config.py`](src/mnemosyne/config.py) for the full list.

## Running the tests

```bash
make test        # 114 tests: unit, integration, API, and failure-mode
make test-cov     # same, with a coverage report (95% at last count)
make typecheck    # mypy, strict on the parts that matter
```

The suite is organized by what it needs to prove:

| Directory              | What it verifies                                                        | DB involved?          |
|-------------------------|---------------------------------------------------------------------------|------------------------|
| `tests/unit/`            | MemoryEngine business rules — validation, versioning logic, status rules | No                     |
| `tests/integration/`     | Repository CRUD/search, service-layer transactions, real Alembic migrations | Yes (in-memory / temp file) |
| `tests/api/`             | Full HTTP request → response round-trips                                  | Yes (in-memory)        |
| `tests/failure/`         | Graceful degradation — malformed input, conflicts, unexpected errors      | Yes (in-memory)        |

## Project layout

```
src/mnemosyne/
├── domain/          Pure entities, enums, exceptions, DTOs — no framework code
├── core/             MemoryEngine: business rules, zero I/O
├── repositories/      MemoryRepository port + SQLite adapter
│   └── sqlite/         ORM models, ORM↔domain mappers, the repository itself
├── db/               Engine/session construction, Unit of Work
├── services/          MemoryService: transaction-per-use-case orchestration
├── api/               FastAPI app, routers, Pydantic schemas, error mapping
├── config.py          Centralized Settings (env-var driven)
├── logging_config.py  Structured JSON logging
└── main.py            Local dev entrypoint

migrations/           Alembic migration environment + versions
tests/                 See table above
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the layers fit together
and why, [DECISIONS.md](DECISIONS.md) for the reasoning behind specific
choices, and [ROADMAP.md](ROADMAP.md) for what comes after Phase 1.

## API overview

Full interactive documentation is auto-generated at `/docs` once the
server is running. Summary:

| Method & Path                                  | Purpose                                    |
|--------------------------------------------------|-----------------------------------------------|
| `POST /api/v1/memories`                            | Create a memory                                |
| `POST /api/v1/memories/batch`                       | Create many memories atomically                 |
| `GET /api/v1/memories`                              | Search/list (keyword, tag, type, date range, confidence/importance range, sort, pagination) |
| `GET /api/v1/memories/{id}`                          | Fetch one (tracks access count)                 |
| `PATCH /api/v1/memories/{id}`                        | Partial update (optionally with optimistic-concurrency `expected_version`) |
| `PATCH /api/v1/memories/batch`                       | Bulk update atomically                           |
| `POST /api/v1/memories/{id}/archive`                  | Archive                                         |
| `POST /api/v1/memories/{id}/restore`                  | Restore from archived or soft-deleted            |
| `DELETE /api/v1/memories/{id}`                        | Soft delete (`?permanent=true` for hard delete)   |
| `GET /api/v1/memories/{id}/versions`                  | List historical (superseded) versions            |
| `GET /api/v1/memories/{id}/versions/{version_number}` | Fetch a specific historical (or current) version |
| `GET /health`                                        | Liveness + DB connectivity check                 |

## How versioning works, briefly

Every update snapshots the memory's *pre-update* state into an
append-only `memory_versions` table before applying the change — the
live `memories` row is never overwritten without a trace. Concurrent
writes are protected two ways: SQLAlchemy's built-in optimistic
concurrency (`version_id_col`) as a safety net for any write, plus an
explicit client-supplied `expected_version` for "only apply this if
nothing has changed since I last read it" semantics. See
[ARCHITECTURE.md](ARCHITECTURE.md#versioning--concurrency) for the full
model and [DECISIONS.md](DECISIONS.md) for why it's built this way.

## Known, deliberate gaps in test coverage

Coverage sits at 95%. The uncovered ~5% is almost entirely generic
`except SQLAlchemyError` defensive branches (disk-full, connection-drop
style infrastructure failures) that would require fault-injection
mocking to trigger — and `main.py`'s `uvicorn.run()` call, which is a
thin, untestable launcher. Every *reachable-through-normal-use* failure
path (duplicate ids, stale versions, not-found, malformed metadata,
invalid input) has an explicit test. See the "Testing" section of
[DECISIONS.md](DECISIONS.md) for more.
