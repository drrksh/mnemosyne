# Architecture

## The shape of the system

```
 surfaces        Claude Code hooks ─┐  MCP server ─┐  CLI ─┐  REST API (FastAPI) ─┐   rule-file exporters
 (adapters)      (+ hook daemon)    │              │       │                      │
                                    ▼              ▼       ▼                      │
 client          Backend: LocalBackend (in-process) │ RemoteBackend (HTTP + API key)
                 · repositories are analysed and transcripts redacted *here*, in either mode
                                    │                                             │
                                    ▼                                             ▼
 composition     Mnemosyne container (container.py): one object wires everything, used by every surface
                                    │
 services        MemoryService · RetrievalService · ContextService · LearningService
                 IdentityService · PortabilityService · audit          (one UnitOfWork per use-case)
                                    │
 engines         core/            retrieval/              learning/               context/
 (no I/O)        memory_engine    documents, bm25,        repo/ analyzers         render (markdown,
                 consolidation    embeddings, index,      conversation/           xml, json)
                 redaction, text  ranker, indexer         heuristics, llm,
                 vocabulary                               transcript; workspace
                                    │
 ports           repositories/interfaces.py: Memory, Evidence, Embedding, Identity, Audit repositories
 adapters        repositories/sqlite/ (SQLAlchemy; runs on SQLite and PostgreSQL)
 storage         db/ (engine, sessions, UnitOfWork, migrations)  ── SQLite file | PostgreSQL
```

**The dependency rule.** Arrows point inward: `domain/` depends on
nothing in the project, and `core/` and `domain/` import no framework
(no SQLAlchemy, FastAPI, Pydantic, NumPy or httpx). You can check this
yourself:

```bash
grep -rn "^from\|^import" src/mnemosyne/core/ src/mnemosyne/domain/ | grep -E "sqlalchemy|fastapi|pydantic|numpy|httpx"
```

It prints nothing.

**One composition root.** The REST API, the CLI, the MCP server, the
hooks and the hook daemon all build the same `Mnemosyne` container, so
behaviour can't drift between surfaces. A memory learned through a hook
is recalled identically through MCP, the API or the CLI. Surfaces use
the container through the `Backend` interface (`client/backend.py`),
which has a local implementation and a remote one. Both pass the same
test suite (`tests/surfaces/test_client_backends.py`).

## Package map

| Package | Responsibility |
|---|---|
| `domain/` | Entities (`Memory`, `MemoryVersion`, `Evidence`, `User`, `Team`, `ApiKey`, `AuditEvent`), enums, `Scope`, `WorkingContext`, `Principal`/`Visibility`, learning DTOs, exceptions |
| `core/` | Pure logic: validation and versioning (`memory_engine`), consolidation decisions, redaction, tokenisation and stemming, the developer vocabulary (tools, slot keys, concepts) |
| `retrieval/` | Embedders (hashing / HTTP / sentence-transformers), BM25F, the in-memory index and its caches, the hybrid ranker, slot resolution, MMR |
| `learning/` | Repository analysis (Python AST, JS/TS, Go, manifests, git history), conversation extraction (heuristics, optional LLM), transcript parsing, workspace detection |
| `context/` | Rendering a context plan as markdown, XML or JSON |
| `services/` | Use-cases and transactions; permissions; audit |
| `repositories/`, `db/`, `migrations/` | Persistence |
| `api/` | FastAPI app, routers, schemas, auth, rate limiting, metrics |
| `client/` | `Backend` (local/remote) and explicit-memory construction |
| `integrations/` | Claude Code hooks, installer, hook daemon; rule-file exporters |
| `cli/`, `mcp_server.py` | Command line and MCP surfaces |
| `evaluation/` | Labeled persona, metrics, evaluation harness |

## Data model

A **memory** is one piece of knowledge about how someone works:

| Field | Meaning |
|---|---|
| `title`, `summary`, `content`, `memory_type` | What it says. Types include `rule`, `preference`, `convention`, `pattern`, `anti_pattern`, `tooling`, `decision`, `architecture`, `procedure`, `failure`, `fact`, `skill` |
| `owner_id`, `team_id` | Personal (team null) or team memory |
| `scope` | Where it applies: `global`, `lang:<language>`, `repo:<host/owner/name>` |
| `key`, `value` | Optional **slot**, e.g. `python.formatter = ruff-format`. Mutually exclusive per (owner/team, scope, key): a new value supersedes the old |
| `origin` | `explicit` > `conversation` > `imported` > `inferred` = `system`. Lower authority never overrides higher |
| `confidence_score`, `importance_score` | Belief and weight; confidence moves with evidence and feedback |
| `reinforcement_count`, `last_reinforced_at` | How often, and how recently, it was observed again |
| `pinned` | Always include in context |
| `fingerprint` | Normalised content hash, for exact-duplicate detection |
| `superseded_by`, `status` | `active`, `archived` (superseded or forgotten), `deleted` |
| `version` | Optimistic-concurrency token; prior states live in `memory_versions` |

**Evidence** (`memory_evidence`) records why a memory exists: kind
(`explicit`, `conversation`, `code`, `commit`, `config`, `feedback`,
`import`, `generalization`), a stable `source_ref` such as
`repo:github.com/acme/api@3f2a…:python-style:python.formatter` or
`session:<id>#<hash>`, an excerpt, and a weight. The pair (memory,
`source_ref`) is unique. Re-ingesting the same observation is a no-op,
which is what makes every extractor safe to re-run.

**Embeddings** (`memory_embeddings`) are stored per memory and model,
with the hash of the text they were computed from, so a stale vector is
detected and recomputed.

**Identity tables:** `users`, `teams`, `team_members` (roles `member` and
`maintainer`), `api_keys` (prefix, SHA-256 hash, scopes, expiry,
revocation), and `audit_events`.

## Tenancy

Every service call carries a `Principal`: user, teams, maintained teams
and permissions. Its `Visibility` (owner plus team ids) becomes a SQL
`WHERE` clause inside the repository (`visibility_clause`), so rows a
caller may not see are never loaded. A memory outside the caller's
visibility raises `NotFoundError` (404), so the API doesn't reveal that
it exists. Writes check `can_write_to(team)`, and permanent deletes check
`can_hard_delete` (owner, team maintainer, or admin). In local mode the
principal is the single local user. In `api_key` mode it comes from the
key (`IdentityService.authenticate`), whose scopes cap its permissions.

## Learning

```
extractors ──► CandidateMemory(+evidence) ──► prepare ──► Consolidator.decide ──► persist ──► IngestReport
 repo analysis       title, content, type,      redact,     vs. slot holder,       create /       "learned 3, reinforced 2,
 conversation        scope, key/value, origin,  validate,   fingerprint match,     version /      changed python.formatter
 LLM (optional)      confidence, importance     threshold   near-duplicates        dynamics       (black → ruff-format)"
 explicit / API / import
```

**Extractors** produce `CandidateMemory` objects carrying their own
evidence:

- **Repository analysis** (`learning/repo/`). Python files are parsed
  with `ast` and `tokenize` (typing, docstrings, naming, imports, error
  handling, logging, test style); JS/TS and Go use bounded heuristics;
  manifests and configuration identify tools, versions and commands; and
  `git log` restricted to the developer's own commits gives commit-message
  and branch conventions. A convention needs enough samples and a
  majority. Its confidence is the Wilson lower bound of the observed
  proportion, so a small sample gives a modest confidence. Evidence
  references include the commit SHA.
- **Conversation heuristics** (`learning/conversation/heuristics.py`)
  run over user turns only, sentence by sentence. They look for
  standing-instruction patterns ("from now on", "always", "never", "I
  prefer", "use X instead of Y", "we decided to … because …") and reject
  task-scoped phrasing ("for this task", "in this file", "for now").
  Named tools map to vocabulary slots.
- **LLM extraction** (`learning/conversation/llm.py`, optional) sends
  the redacted user turns to Claude with a JSON schema. A finding is kept
  only if its supporting quote appears verbatim in a user turn. If both
  extractors found the same sentence, it counts once.

**Preparation** redacts title, content, value and evidence, drops
mostly-secret candidates, validates scope and team membership, and
applies the minimum confidence (`MNEMOSYNE_LEARNING_MIN_CONFIDENCE`).
Explicit statements are exempt from the confidence threshold.

**Consolidation** (`core/consolidation.py`, pure) decides one action per
candidate:

| Situation | Action |
|---|---|
| Evidence `source_ref` already recorded | `skipped` (idempotent) |
| Same belief: slot held by the same value, same fingerprint, or a near-duplicate (cosine ≥ 0.92) of the same type and polarity | `reinforced`: confidence rises toward its ceiling with diminishing steps; count and timestamp updated. A more authoritative candidate also takes over the memory's origin and wording ("the developer confirmed what was inferred") |
| Contradiction: slot held by a different value, or a near-duplicate with opposite polarity ("always X" / "never X"), candidate at least as authoritative | `superseded`: the old memory is archived with `superseded_by`, the new one created, history kept |
| Contradiction from a less authoritative source | `kept_existing`: the candidate's evidence is attached to the existing memory, and an inferred memory loses some confidence. An explicit memory is never weakened by inference |
| Otherwise | `created` |

Belief changes (reinforcement, feedback, access) use a dynamics update
that does **not** create a version, because versions record *edits*.
Edits (supersession, archive, explicit updates) snapshot the prior state
first.

**Generalization** promotes a slot value to a general preference when it
appears in at least `MNEMOSYNE_GENERALIZE_MIN_REPOS` (default 2)
repositories that the developer meaningfully commits to (author share ≥
20%), with agreement of at least 60%. Origin is `system`, so anything the
developer says explicitly overrides it.

**Feedback.**

| Signal | Effect |
|---|---|
| `helpful` | Raises confidence and importance, and counts as a reinforcement |
| `confirm` | Makes the memory explicit, with confidence at least 0.95 |
| `not_helpful` | Lowers importance, so the memory surfaces less without being called false |
| `wrong`, `outdated` | Archive the memory: it stops being used, stays restorable, and is recorded in its history |

Every signal is also stored as evidence, with the developer's note.

## Retrieval

**Index.** For a given visibility, `RetrievalService` builds a
`RetrievalIndex` over active memories. It holds three parts:

- a BM25F index over weighted fields (title, key/value, tags, summary,
  content), tokenised by a code-aware tokenizer that splits identifiers,
  stems lightly and normalises lemmas;
- an embedding matrix (L2-normalised rows);
- per-memory developer concepts (testing, error handling, persistence,
  and so on) from `core/vocabulary.py`.

**Caching.** An index is reused while the database's `IndexSignature`
(active count, max `updated_at`, sum of versions) is unchanged. That
check is one cheap query per request, so a write through any process
invalidates every cache. Short-lived processes load an on-disk snapshot
validated by the same signature, through an allow-listed unpickler.

**Ranking** (`retrieval/ranker.py`):

```
relevance = (1 − c)·[s·semantic + (1 − s)·lexical] + c·concept          s = 0.5, c = 0.1
score     = (0.70·relevance + 0.12·importance + 0.08·confidence
             + 0.07·recency + 0.13·scope + 0.05·reinforcement) / Σweights
```

- **Scope** is the affinity of the memory's scope to the working
  context: same repository 1.0, same language 0.85, global 0.6. Memories
  scoped to another repository or language are excluded outright.
- **Recency** halves every 90 days since the memory was last confirmed,
  reinforced or used. It's quantised so that seconds between two calls
  can't reorder results.
- **Ties** break on memory id, never on load order. The same inputs
  produce the same order in every process, which also keeps an
  assistant's prompt cache warm.
- **Relevance floor.** Memories below `min_relevance` (0.06) are not
  "about" the query and are dropped from recall.
- **Slot resolution.** Of several memories for the same slot, keep the
  most specific scope (repository > language > global), then personal
  over team, then higher confidence.
- **Diversity.** MMR removes near-duplicates from the top results.

Every result carries a `ScoreBreakdown` (`--explain`, `"explain": true`).

## Context compilation

`ContextService.compile` (`services/context_service.py`) builds what an
assistant receives:

1. **Relevant layer.** Recall against the task, keeping memories within
   40% of the best match's relevance, and only if they are strongly
   relevant (≥ 0.25) or important (≥ 0.5). A weak tail costs the model
   attention.
2. **Profile layer.** Standing knowledge for the working context: rules,
   anti-patterns, conventions and tooling with slots, important
   preferences, repository commands and facts, skills, and pinned
   memories (`is_profile_memory`).
3. **Packing.** Slots are resolved across both layers. Then the token
   budget is packed greedily: the relevant layer gets first claim on its
   share, the profile gets `MNEMOSYNE_CONTEXT_PROFILE_SHARE` (45%), and
   whatever one layer leaves unused flows to the other.
4. **Rendering.** Markdown (sectioned: Relevant to this task, Rules,
   Preferences, Conventions, Tooling and commands, Project knowledge,
   Lessons learned…), XML or JSON. Optionally wrapped in
   `<mnemosyne-context>`, and capped at `max_chars` (hooks).

## Surfaces

- **REST API** (`api/`). Routers for memories, context and recall,
  learning, account (keys, export, import, erasure, audit) and
  administration. Middleware assigns request ids, records Prometheus
  metrics by route template, and rejects oversized bodies. Domain errors
  map to HTTP statuses in one place (`error_handlers.py`).
- **CLI** (`cli/`). Click commands over a `Backend`. Administration,
  migrations and evaluation act on the local database directly. Logging
  goes to stderr, never stdout, so `hook` and `mcp` output stays clean.
- **MCP server** (`mcp_server.py`). FastMCP over stdio, with seven tools,
  a profile resource and usage instructions. Tested end to end with the
  official client (`tests/surfaces/test_mcp_stdio.py`).
- **Claude Code hooks** (`integrations/claude_code.py`):
  - `SessionStart` injects the profile.
  - `UserPromptSubmit` learns from the prompt and injects relevant
    memories.
  - `PreCompact` and `SessionEnd` spawn a detached learner.
  - The installer merges into `settings.json`, keeps a backup, and is
    idempotent and reversible.
- **Hook daemon** (`integrations/hook_daemon.py`). Claude Code runs every
  hook as a new process, and imports dominate (~0.7 s against ~50 ms of
  work). A per-user daemon holds the container warm behind a Unix socket
  (`0600` in a `0700` directory; the client verifies ownership). It's a
  single instance per home (`flock`), and exits when idle, on config
  change or on upgrade. Hooks fall back to in-process handling whenever
  it isn't reachable.
- **Rule-file exporters** (`integrations/exporters.py`). A managed block
  in `CLAUDE.md`, `AGENTS.md`, Copilot and Gemini files, and a whole
  Cursor `.mdc` file. Repository and team knowledge only by default.

## Versioning and concurrency

The `memories` row is always the current state. `memory_versions` holds
only *superseded* states. An edit snapshots the pre-edit state and
applies the change in the same transaction. Concurrency is protected two
ways:

- SQLAlchemy's `version_id_col`: every ORM `UPDATE` matches the loaded
  version, and a lost race becomes `ConflictError`.
- An optional client-supplied `expected_version`, for "only if nothing
  changed since I read it".

Access tracking and belief dynamics use Core `UPDATE` statements that
deliberately do not bump the version. Reading a memory is not an edit.

## Transactions

Each use-case opens one `UnitOfWork`, makes its repository calls, writes
its audit event, and commits once. Leaving the block via an exception
rolls everything back, so a batch with one invalid item leaves no partial
writes.

## Storage and migrations

The SQLAlchemy models run unchanged on SQLite (local) and PostgreSQL
(servers). SQLite connections get WAL mode, a busy timeout, and enforced
foreign keys. Migrations ship inside the package (`mnemosyne/migrations`)
and run programmatically (`db/migrate.py`): at start-up when
`MNEMOSYNE_AUTO_MIGRATE=true`, or via `mnemosyne db upgrade`.

The current head revision is pinned in `migrate.HEAD_REVISION`, so the
per-process schema check doesn't import Alembic; a test keeps the pin
honest. Another test applies every migration to a fresh database and
asserts the result matches the ORM models exactly, on SQLite and, in CI,
on PostgreSQL.

## Observability

- **Logging.** Structured JSON (or console) via the standard library,
  with a request id on every line.
- **Metrics.** Prometheus counters and latency histograms by method,
  route template and status. Labels use templates, so cardinality is
  bounded.
- **Health.** `/health` (liveness and database), `/ready` (schema
  revision).
- **Hook log.** Hooks log one line each to `~/.mnemosyne/logs/hooks.log`
  with duration and outcome. The detached learner and the daemon log to
  `learner.log` and `daemon.log`.

## Extension points

| To add | Attach at |
|---|---|
| A language analyzer | `learning/repo/`, called from `RepositoryAnalyzer`; emit `Finding`s with vocabulary slot keys |
| A tool or slot | `core/vocabulary.py` (tool table, categories, aliases) |
| An extractor (IDE plugin, ticket system, code review) | Produce `CandidateMemory` objects, or call `POST /api/v1/learn/candidates` |
| An embedder | `retrieval/embeddings.py` (`Embedder` protocol); the model id is part of the index key |
| An assistant integration | A `Backend` user: hooks, MCP, a rule-file target in `exporters.TARGETS` |
| A storage backend | Implement the ports in `repositories/interfaces.py` |
