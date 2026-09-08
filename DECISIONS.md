# Decision Log

Each entry: the problem, the options considered, what was chosen, why,
and what it costs. Written so a future maintainer (or a future phase's
author) can tell *why* something looks the way it does before deciding
whether to change it.

---

### 1. Database: SQLite + SQLAlchemy + Alembic, with an explicit PostgreSQL path

**Problem:** the brief mandates SQLite for Phase 1 but requires the
architecture to support PostgreSQL later without a rewrite.

**Chosen:** SQLAlchemy 2.0 (typed declarative style) as the only thing
that talks to the database, Alembic for migrations with
`render_as_batch=True` unconditionally enabled.

**Reasoning:** `render_as_batch` makes Alembic rebuild-and-swap tables
for schema changes instead of emitting raw `ALTER TABLE`, which is
required for SQLite (it can't alter most column properties in place)
and is a documented no-op on backends with native `ALTER` support. This
means the *same* migration files work against PostgreSQL later with no
edits — the alternative (SQLite-only migrations, rewritten for
PostgreSQL when the time comes) would silently violate the brief's
"without major redesign" requirement the first time someone actually
tried the migration.

**Trade-off:** batch mode is marginally slower for large SQLite tables
(full rebuild vs. in-place alter) — irrelevant at Phase 1 scale, worth
watching if a single table exceeds tens of millions of rows before the
PostgreSQL migration happens.

---

### 2. Repository + Unit of Work pattern, with the repository injected

**Problem:** business logic must never depend on the database
implementation.

**Chosen:** `MemoryRepository` as an abstract port
(`repositories/interfaces.py`), `SqliteMemoryRepository` as its only
adapter today, and `UnitOfWork` takes the repository implementation as
a constructor parameter (defaulting to `SqliteMemoryRepository`) rather
than hardcoding it.

**Reasoning:** this is the standard, well-understood way to get
Dependency Inversion without inventing anything novel. The injection
point on `UnitOfWork` specifically (rather than only on `MemoryService`)
means a decorator-style repository (caching, encryption — see
ROADMAP.md) can wrap the SQLite implementation without `MemoryService`
changing at all.

**Trade-off:** an extra layer of indirection for a project that, today,
has exactly one repository implementation. Judged worth it because the
brief explicitly asks for this property and because the cost is small
— `UnitOfWork` is 75 lines.

---

### 3. Memory type & status as string enums, not native database enums

**Problem:** `MemoryType` needs to be "easily extendable"; PostgreSQL's
native `ENUM` type requires `ALTER TYPE ... ADD VALUE` (which can't run
inside a transaction in older PostgreSQL versions) to add a value, and
SQLite has no enum type at all.

**Chosen:** plain Python `str, Enum` classes, stored as `VARCHAR` with
an application-level `CheckConstraint` (`status IN (...)`) for the
small, truly-fixed `MemoryStatus` set, and no DB-level constraint at
all for the open-ended `MemoryType` set.

**Reasoning:** adding a new `MemoryType` becomes: add one line to the
enum, run a normal (already-portable) migration if you want a
DB-level check — never a special enum-altering operation. `MemoryStatus`
gets a `CheckConstraint` because its three values are a closed,
stable set with real business meaning (the status-transition state
machine depends on exactly these three); `MemoryType` doesn't, because
the whole point is that it grows.

**Trade-off:** an invalid `MemoryType` string written directly to the
database (bypassing the application layer entirely) wouldn't be caught
by a DB constraint — only by the domain-layer validation in
`MemoryEngine._validate_memory_type`. Acceptable: nothing outside this
codebase should be writing to these tables directly.

---

### 4. Tag storage: normalized (join table) vs. JSON array

**Problem:** tags need to support indexed, efficient search — "Tag
Search" is an explicit requirement — and the brief asks to avoid
N+1 queries and favor proper indexing.

**Chosen:** a `tags` table plus a `memory_tags` join table, not a JSON
array column on `memories`.

**Reasoning:** a JSON array can't be indexed for "contains" queries the
way a join table's foreign key can; searching "any of these 3 tags"
against a JSON column means a full scan-and-parse of every row, while
the join-table version is a straightforward indexed `WHERE tag_id IN
(...)` with a `GROUP BY ... HAVING COUNT(...)` for "all of these tags."
The `lazy="selectin"` relationship loading strategy avoids the N+1
problem the brief specifically calls out (one extra query to load every
memory's tags in a batch, not one query per memory).

**Trade-off:** every write to a memory's tags does a get-or-create
lookup against the `tags` table (see
`SqliteMemoryRepository._resolve_tags`) instead of a single JSON
write. More moving parts for a genuinely more scalable read path —
the right trade for a system meant to support millions of memories.

---

### 5. Versioning: append-only, superseded-states-only, not full event sourcing

**Problem:** "never overwrite information" — every update needs full
history, and "what did this look like 3 months ago" must be
answerable.

**Chosen:** `memories` holds live current state; `memory_versions`
holds only *prior, superseded* snapshots, one row per supersession,
each tagged with which fields changed and why.

**Reasoning:** a full event-sourcing design (memories reconstructed
entirely from a stream of events, no "current state" table at all)
would satisfy "never overwrite" too, but at real cost: every read
would need to replay history, and it is significantly more machinery
than Phase 1 needs. The chosen design gets the same audit guarantee
(nothing is ever silently lost) while keeping reads O(1) — exactly the
kind of "avoid unnecessary complexity" trade the brief asks for
explicitly. `MemoryService.get_version()` synthesizes a
version-shaped view of the *current* state on demand for the version
number that has no row yet (see the docstring there) — so callers
never need to know which version numbers are "real" rows versus the
live row.

**Trade-off:** reconstructing "the full state of the memory store as
of timestamp X" (as opposed to one memory's history) would require
walking every memory's version history rather than one time-partitioned
query. Not a Phase 1 requirement; noted for whoever tackles it.

---

### 6. Optimistic concurrency: SQLAlchemy's `version_id_col`, not a hand-rolled check

**Problem:** prevent two concurrent writers from silently clobbering
each other ("Race conditions" is explicitly listed under Error
Handling).

**Chosen:** SQLAlchemy's built-in `version_id_col` mapper argument,
plus a separate, explicit `expected_version` check at the service
layer for client-driven "only if unchanged since I read it" semantics.

**Reasoning:** `version_id_col` is a well-tested, fifteen-plus-year-old
feature of the ORM already in use — reimplementing manual
`WHERE version = ...` / `UPDATE ... RETURNING` logic by hand would be
more code with a higher chance of a subtle bug (the `version` field
also happens to be the field the brief wants exposed to clients as
"which revision is this," so tying the two together — rather than
maintaining a separate internal-only concurrency token — was the
simpler design). The explicit `expected_version` on top is a distinct
concern: it's about client intent ("reject if this doesn't match what
I last saw"), not just about safety inside one request.

**Trade-off:** none significant — this is close to the textbook use
case for `version_id_col`.

---

### 7. Soft delete and hard delete as two distinct, explicit operations

**Problem:** the brief lists both "Soft Delete" and "Permanent Delete"
as required CRUD operations.

**Chosen:** `DELETE /memories/{id}` soft-deletes by default (sets
`status=deleted`, `deleted_at=now()`, fully versioned and restorable);
`DELETE /memories/{id}?permanent=true` hard-deletes (removes the row
and its entire version history — genuinely gone).

**Reasoning:** these are semantically different operations with
different audit implications, so they get different code paths rather
than one method with a boolean that silently changes what "delete"
means. Hard delete purging version history too (rather than keeping an
orphaned audit trail) was a deliberate choice: "permanent" should mean
permanent, and a system that claims to hard-delete something while
quietly keeping its history in another table would be misleading about
what it actually does.

**Trade-off:** if a future compliance requirement needs "hard-deleted,
but we still need an audit trail that it *happened*," that's a
different feature (e.g., a separate deletion-log table) — not
something this design currently provides.

---

### 8. `UNSET` sentinel for PATCH semantics

**Problem:** a PATCH request needs to distinguish "the client didn't
mention this field" (leave it alone) from "the client explicitly set
this field to null/empty" (clear it) — a plain dict or a dataclass with
`None` defaults can't tell these apart.

**Chosen:** a singleton `UNSET` sentinel as the default value for every
field on `MemoryUpdateData`; Pydantic's `model_fields_set` on the
inbound request tells the API layer which fields to actually copy over.

**Reasoning:** this is a well-known, standard solution to a real
ambiguity — the alternative (accepting the ambiguity, e.g. treating
"field omitted" and "field null" identically) would make it impossible
to ever clear a nullable field like `source` via PATCH without
resending every other field too.

**Trade-off:** one extra concept for anyone reading `domain/dto.py`
for the first time; documented inline where it's defined.

---

### 9. FastAPI + Pydantic v2

**Problem:** need a REST API layer with strong validation.

**Chosen:** FastAPI for routing/DI, Pydantic v2 for request/response
schemas and validation.

**Reasoning:** the brief explicitly asks for auto-documented endpoints
implicitly (via "API Endpoints" under Documentation) and strong typing
throughout; FastAPI generates a full OpenAPI spec and interactive
`/docs` UI from the same type annotations used for validation, so the
documentation can't drift from the actual accepted shapes the way
hand-maintained API docs can.

**Trade-off:** none significant for this project's needs.

---

### 10. Structured JSON logging via the standard library, not a third-party package

**Problem:** every operation needs structured, observable logging.

**Chosen:** Python's built-in `logging` module with a custom JSON
`Formatter` and a `ContextVar`-based request-id filter, rather than
`structlog` or similar.

**Reasoning:** one fewer third-party dependency to keep compatible
across a stated "years" timeline, and the standard library's `logging`
is already the interface every other Python library in the stack
(SQLAlchemy, Alembic, uvicorn) logs through — using anything else would
mean two logging systems to configure and reconcile.

**Trade-off:** a dedicated structured-logging library has more
built-in ergonomics (bound loggers, better context propagation
primitives). If log volume or query needs grow substantially, this is
a contained, low-risk thing to swap later — every call site is just
`logging.getLogger(...)`.

---

### 11. Configuration strategy: env vars via `pydantic-settings`, resolved once at import time where needed

**Problem:** "no magic numbers," every limit configurable, and FastAPI
route/Pydantic field declarations need their constraint values (e.g.
`max_length`) at class-definition time — before any request-scoped
dependency injection has run.

**Chosen:** a single `Settings` object (`config.py`), loaded once via
`get_settings()` (`lru_cache`d), imported directly at module level in
`api/schemas.py` and `api/routers/memories.py` wherever a Pydantic
`Field(...)` or FastAPI `Query(...)` needs a default/bound.

**Reasoning:** this is the most direct way to keep every limit
genuinely centralized in `config.py` while satisfying Pydantic/FastAPI's
requirement that these values exist at class/route-definition time.

**Trade-off:** these particular values are effectively fixed for the
life of the process — changing an env var and expecting an
already-imported `MemoryCreateRequest.title`'s `max_length` to change
without a restart won't work. Acceptable for a single-process Phase 1
deployment; worth revisiting if a future phase adds live-reloadable
configuration. Genuine domain constants (confidence/importance scores
being bounded to `[0.0, 1.0]`) are deliberately *not* run through this
mechanism — that range is a property of what a "unit score" means, not
an operational tuning knob, and routing it through config would be
indirection without benefit.

---

### 12. Metadata: sanitize-and-drop instead of hard failure

**Problem:** the brief explicitly requires "If metadata is malformed:
Ignore metadata, Preserve core memory."

**Chosen:** `MemoryEngine._sanitize_metadata` attempts to JSON-serialize
the metadata and checks it against a configurable size limit; on either
failure, it logs a warning and returns `{}`, and the rest of the
create/update proceeds normally.

**Reasoning:** this is a direct, literal implementation of the
requirement, tested in `tests/failure/test_failure_modes.py`. A
memory's title/content/type carry the actual information; its metadata
is auxiliary, so degrading it gracefully rather than rejecting the
whole write is the right trade specifically because the brief says so
explicitly, not just as a general design preference.

**Trade-off:** a caller with a genuine bug producing bad metadata gets
silent data loss (of the metadata only) rather than a loud error. The
warning-level log line is the intended mitigation — this is a
deliberate, spec-driven choice, not an oversight.

---

### 13. Deliberate deviation: fail-fast on migration/index problems rather than "continue with a warning"

**Problem:** the brief's Fallback Systems section asks for "If indexes
fail: Continue functionality, generate warning" and "If migration
fails: Rollback, restore previous schema, generate diagnostics."

**Chosen:** migrations are transactional and either fully succeed or
fully roll back (Alembic's normal behavior, confirmed by
`tests/integration/test_migrations.py`), and there is deliberately no
code path that catches an index-creation failure and continues without
it.

**Reasoning:** this is the one place this implementation intentionally
diverges from the literal brief, and it's flagged as an "autonomy"
improvement per the brief's own invitation to do so. A production
system that silently continues after failing to create an index it
expected to have would degrade — slower queries, maybe eventually
timeouts — in a way that's invisible until someone notices the
slowdown days later and has to reverse-engineer why. Failing the
deploy/migration loudly, immediately, when the schema doesn't match
expectations is the safer default; the operator finds out *before* it
affects users, not after. A future phase could add an explicit,
opt-in "best-effort index creation" mode for specific low-stakes
indexes if a real use case for it shows up — right now it doesn't
seem to exist, and adding a foot-gun for a use case nobody has yet
seemed like the wrong instinct.

**Trade-off:** a transient environment issue during migration
(disk I/O hiccup) now fails the deploy rather than degrading silently
— judged to be exactly the correct behavior, not a cost.

---

### 14. Testing: real integration coverage over mocking the database

**Problem:** the brief wants CRUD, integration, failure, validation,
migration, and edge-case tests with "high test coverage."

**Chosen:** almost every test below the unit layer runs against a real
SQLite database (in-memory for speed, a real temp-file database
specifically for the Alembic migration tests) rather than mocking
SQLAlchemy. 114 tests, 95% line coverage.

**Reasoning:** the bugs that actually surfaced during development
(see below) were exactly the kind that mocking would have hidden — a
missing `Base.metadata.create_all()` call and a service method
returning a stale in-memory snapshot instead of the freshly-updated
one. Both were caught because the tests exercise real SQLAlchemy
sessions and a real (if in-memory) database, not a mock that would
have happily returned whatever it was told to.

**Trade-off:** the remaining ~5% of uncovered lines are generic
`except SQLAlchemyError` branches for infrastructure failures (disk
full, connection drop) that would need fault-injection mocking to
trigger — judged not worth contriving a fake failure just to paint a
line green, versus the value of the 95% that's testing real behavior
against a real database.
