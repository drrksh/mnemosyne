# Decision Log

Each entry: the problem, the options considered, what was chosen, why,
and what it costs. Written so a future maintainer (or a future phase's
author) can tell *why* something looks the way it does before deciding
whether to change it.

Decisions 1–14 date from Phase 1 (a generic local memory engine).
Decisions 15 onward record turning it into a developer-memory product.
Where a later decision amends an earlier one, the earlier entry says so.

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

> **Amended by decision 25:** configuration no longer comes from a `.env`
> file in the working directory.

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

---

## Developer memory (decisions 15 onward)

### 15. Specialise in how developers work, rather than general "memory"

**Problem:** a general memory engine has no opinion about what matters.
To make an assistant behave like it knows a developer, the system has to
know what a convention, a tool choice, a decision and a lesson look like,
and where each applies.

**Chosen:** a developer vocabulary. The memory types are `convention`,
`pattern`, `anti_pattern`, `tooling`, `skill`, `decision`, `procedure`
and `failure`. On top of them sit scopes (`global`, `lang:x`, `repo:id`),
slots for settings that hold exactly one value (`python.formatter`), and
a curated tool and concept vocabulary (`core/vocabulary.py`) shared by
the extractors and the ranker.

**Trade-off:** the vocabulary needs maintenance as ecosystems change, and
knowledge it doesn't cover falls back to free text, which supersedes
nothing and is retrieved only by similarity.

### 16. Origin authority, and merging instead of appending

**Problem:** the same knowledge arrives many times from many sources, and
sources disagree. An append-only store gets noisier the more it learns.

**Chosen:**
- Every incoming observation goes through one pure `Consolidator` that
  reinforces, supersedes, keeps the existing memory, or creates a new
  one (ARCHITECTURE.md, "Learning").
- Authority is fixed: `explicit` > `conversation` > `imported` >
  `inferred` = `system`. Lower authority never overwrites higher.
- Belief changes (reinforcement, feedback, access) don't create
  versions. Versions record edits.

**Why:** what the developer said must beat what was guessed, and saying it
again should strengthen a memory, not duplicate it. Keeping "how
strongly we believe this" apart from "what this says" keeps version
history readable.

**Trade-off:** a correct inference can't displace a stale explicit
statement on its own. It is kept as counter-evidence until the developer
confirms or corrects (`feedback`).

### 17. Evidence is first-class, and idempotent

**Chosen:** every memory carries evidence rows with a stable `source_ref`
(commit SHA plus analyzer plus key, session id plus sentence hash, and so
on), unique per memory.

**Why:** "why does it think that?" must always have an answer. Idempotent
references also make every extractor safe to re-run: hooks fire twice
(`PreCompact`, then `SessionEnd`), backfills overlap, imports repeat.

**Trade-off:** an evidence table that grows with use. It's capped per
memory (`MNEMOSYNE_EVIDENCE_CAP_PER_MEMORY`).

### 18. Hybrid retrieval, with a zero-dependency embedder by default

**Problem:** good retrieval usually means a neural embedding model, which
means a large dependency, a GPU or a paid API, and sending code knowledge
off the machine.

**Chosen:** retrieval combines BM25F over memory fields, signed feature
hashing (a deterministic, offline "embedder"), and a developer-concept
layer, blended with importance, confidence, recency, scope and
reinforcement. Neural embedders plug in (`http` for any OpenAI-compatible
endpoint, or `sentence-transformers`). Quality is measured on a labeled
persona, with held-out queries written *before* tuning (docs/EVALUATION.md).

**Why:** developer memories are short and full of exact identifiers (tool
names, flags, file conventions), where lexical signals are strong. The
concept layer gives the "write tests" → testing-conventions association
that matters most. Measured: in-sample nDCG@5 0.87, held-out 0.82, zero
cross-repository leakage.

**Trade-off:** open-domain synonymy ("refund" → "money handling") is weak
without a neural embedder. The neural options are implemented but have
not been measured on the persona.

### 19. Deterministic ranking

**Problem:** the hook-latency benchmark showed the same profile rendering
in different orders in two processes. Memories with equal scores were
ordered by load order and by seconds of recency jitter.

**Chosen:** recency is quantised to 0.001 (it's a day-scale signal), and
exact ties break on memory id (`RetrievalIndex.id_rank`).

**Why:** the same inputs must give the same context. Users notice when it
doesn't, and assistants' prompt caches only hit on byte-identical
prefixes.

**Trade-off:** none measurable. Retrieval metrics are unchanged.

### 20. Two-layer context: relevant first, profile second

**Chosen:** the compiled context has two layers. A task-relevant layer is
kept to the strong part of the ranking (within 40% of the best match,
and strongly relevant or important). An always-on profile layer holds
rules, conventions, tooling and repository commands for the working
context. Both share one token budget (profile share 45%, unused budget
flows across).

**Why:** some knowledge applies to every task in a repository ("named
exports only"), yet is *about* no particular task, so recall ranks it
low. Other knowledge matters only when relevant. Injecting the weak tail
of a ranking costs the model attention. Measured: 100% of essential
memories in-sample, 93% held-out, at 1,500 tokens.

### 21. Conversation learning is precision-first, and only hears the developer

**Chosen:**
- Heuristics extract only standing statements from **user** turns, and
  reject task-scoped phrasing.
- Assistant turns, tool output and injected context are never mined.
- The optional LLM extractor must ground each finding in a verbatim user
  quote.

**Why:** a wrong memory is worse than a missing one, because it is
repeated in every future session. Mining only what the developer typed
also closes the main prompt-injection path: a web page or file an
assistant reads can't become a memory.

**Trade-off:** recall of implicit preferences is limited without the LLM
extractor.

### 22. Repository analysis is statistical, local, and never executes code

**Chosen:**
- Analyzers count, and a convention needs enough samples and a majority.
- Confidence is the Wilson lower bound of the observed proportion.
- Personal habits (commit style) come only from the developer's own
  commits.
- Python is parsed with `ast`; nothing in the repository is run.
- In team mode, analysis still happens on the developer's machine and
  only findings are sent.

**Why:** "88% of functions are annotated" is evidence; one file is not.
Running repository code (build scripts, test discovery) would turn
"learn this repo" into "execute this repo".

### 23. Local-first, with an optional team server behind the same interface

**Chosen:** one `Backend` interface with two implementations.
`LocalBackend` uses an in-process container on SQLite. `RemoteBackend`
calls the REST API with an API key. Every surface uses the interface,
and both implementations pass the same tests.

**Why:** a single developer should get value with zero infrastructure,
while a company needs shared team knowledge, central administration and
audit. Keeping one interface means features don't fork.

**Trade-off:** some operations need a server endpoint before they work
remotely (for example `/rules` for `sync`, `/me/stats` for `status`).

### 24. Tenancy in the query, and 404 for invisible

**Chosen:** a caller's `Visibility` becomes a SQL `WHERE` clause in the
repository layer. Invisible memories raise `NotFoundError`.

**Why:** filtering after loading is one forgotten check away from a leak.
Returning 403 would confirm that a memory exists.

### 25. Configuration never comes from the working directory

> Amends decision 11.

**Problem:** hooks and the MCP server run inside whatever repository is
open. If a `.env` in the current directory were read, cloning a
malicious repository could set `MNEMOSYNE_SERVER_URL` and send the
developer's memory and prompts elsewhere.

**Chosen:** settings come only from environment variables,
`<home>/config.env`, and a file explicitly named by
`MNEMOSYNE_CONFIG_FILE`. A regression test plants a hostile `.env`.

**Trade-off:** the `.env` convenience is gone for local development of
the server. Use `MNEMOSYNE_CONFIG_FILE=.env`.

### 26. Hooks never fail, and do slow work out of band

**Chosen:**
- Every hook handler is wrapped: any error is logged and the hook exits
  0 with no output.
- Transcript learning runs in a detached process.
- Injected context is capped below Claude Code's 10,000-character limit,
  and wrapped in `<mnemosyne-context>` so it is never re-learned.

**Why:** a memory tool that breaks or slows the editor gets uninstalled.
`SessionEnd` hooks share a 1.5-second budget (raised to a longer per-hook
timeout, up to 60 seconds) while Claude Code exits.

### 27. A per-user hook daemon on a Unix socket

**Problem:** Claude Code starts a process per hook, and `UserPromptSubmit`
runs before every prompt. Profiling showed about 0.7 s per hook, almost
all of it imports. The work itself was about 50 ms.

**Options:** (a) trim imports, which saves perhaps 30%; (b) a long-running
local HTTP server on a TCP port; (c) a per-user daemon on a Unix socket,
started on demand.

**Chosen:** (c).
- The first hook answers in-process and starts the daemon. Later hooks
  hand the event over using only the standard library.
- The filesystem is the access control: the socket is `0600` in a
  `0700` directory, and the client refuses sockets it does not own.
- The daemon is disposable: one per home (`flock`), and it exits when
  idle, on config change, or on upgrade.
- Any failure falls back to in-process handling.

**Why:** p50 went from ~730 ms to ~100 ms with identical output. A TCP
port would be reachable by every local user and process, and need its
own authentication.

**Trade-off:** POSIX only. On Windows hooks work, without the speed-up.

### 28. Rule files carry repository knowledge, not personal preferences

**Chosen:** `mnemosyne sync` writes only repository- and team-scoped
memories by default, inside a marked block it owns (or a file it owns,
for Cursor).

**Why:** `CLAUDE.md` and `AGENTS.md` are usually committed. One
developer's "keep answers terse" shouldn't become a teammate's
instruction. Personal context flows through hooks and MCP instead.

### 29. API keys: random secrets, SHA-256, prefix lookup

**Chosen:** keys look like `mn_<12-hex prefix>_<32-byte URL-safe
secret>`. They are stored as SHA-256 and looked up by prefix, with a
constant-time comparison. An unknown prefix is compared against a dummy
hash.

**Why:** password hashing (bcrypt, argon2) exists to slow guessing of
low-entropy secrets. These secrets have 256 bits of entropy, so a fast
hash is sufficient, and it keeps authentication off the latency budget.

### 30. Migrations ship in the package, and the head revision is pinned

**Chosen:**
- Alembic scripts live in `mnemosyne/migrations` and run
  programmatically (`db/migrate.py`).
- The head revision is pinned in `migrate.HEAD_REVISION`, so the schema
  check at process start doesn't import Alembic.
- Tests check the pin against the files, and the migrated schema against
  the ORM models, on SQLite and PostgreSQL.

**Why:** `pip install` must yield a working `mnemosyne db upgrade` with
no checkout. Every hook process checks the schema, so the check must be
cheap.
