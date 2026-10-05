# Mnemosyne

**An engineering memory for AI coding assistants.** Mnemosyne learns how
a developer builds software: their conventions, tools, decisions, and the
lessons that cost them an outage. Before any assistant writes a line, it
gives that assistant a short, relevant briefing. The developer never has
to re-explain themselves, and every assistant they use (Claude Code,
Cursor, Copilot, Gemini, anything that speaks MCP) gets the same picture.

```
$ mnemosyne context "add an endpoint to refund a payment"

## Developer memory (Mnemosyne)
Working in repository `github.com/acme/payments-api` — python.

### Relevant to this task
- Idempotency keys on POST endpoints — Every POST endpoint accepts an Idempotency-Key header
  stored for 24 hours so client retries are safe; decided after the double-charge incident.
- Flaky refund concurrency test — fixed by wrapping each test in a transaction that is rolled back.
- Authorization with JWT scopes — endpoints check permissions with require_scope('payments:admin').

### Rules
- Money as integer minor units
- Never hardcode secrets

### Conventions
- Async HTTP with httpx
- Factory fixtures for test data
- Table-driven tests with parametrize
...
```

That briefing came from 101 memories across four repositories, in a few
milliseconds once the index is warm. Nothing from the other three
repositories leaked in, and the task-relevant lessons came first.

---

## How it works

```
   learn                         remember                        deliver
   ─────                         ────────                        ───────
   your repositories ──┐                                    ┌──► Claude Code hooks (automatic)
   (code, config, git) │     ┌──────────────────────────┐   ├──► MCP tools (Cursor, Claude, …)
   your AI sessions ───┼───► │ memories with evidence,  │ ──┼──► CLAUDE.md / AGENTS.md / Copilot /
   what you tell it ───┤     │ scope, confidence, slots │   │    Cursor / Gemini rule files
   your feedback ──────┘     └──────────────────────────┘   └──► REST API, CLI
```

**Learning.** Mnemosyne learns from four sources.

- **Repository analysis.** Language-aware analyzers for Python,
  TypeScript/JavaScript and Go read code style, typing, testing patterns,
  error handling and logging. They also read manifests and configuration
  (package managers, frameworks, linters, CI, how to run things) and the
  developer's *own* commits (message style, branch names). Every finding
  carries a confidence (a Wilson lower bound over the samples seen) and
  evidence pointing at the commit it came from.
- **Conversations.** As you work, each prompt is checked for lasting
  statements ("no, use pnpm instead of npm", "never mock the database
  here"). Whole session transcripts are mined when a session compacts or
  ends. This is precision-first: "use pnpm for this task" is not stored.
  An optional LLM extractor (Claude) catches subtler preferences and must
  quote the user verbatim, or its finding is discarded.
- **Explicit statements.** `mnemosyne remember "…"`, or an assistant
  calling the `remember` tool.
- **Feedback.** Mark a memory helpful, wrong or outdated, or confirm it.
  Confirming makes an inferred memory authoritative.

**Consolidation.** Incoming knowledge is merged, not appended.

- **Repeats.** Seeing the same thing again reinforces it.
- **Slots.** "Python formatter", "JS package manager" and similar settings
  hold one value per scope. A newer statement supersedes the old one and
  keeps the history.
- **Authority.** What you said beats what was inferred:
  explicit > conversation > imported > inferred.
- **Contradictions** are resolved by authority. An inference that
  contradicts something you said is kept as counter-evidence but never
  overrides it.
- **Generalization.** A convention you follow across several of your own
  repositories becomes a general preference.

**Delivery.** Scope decides what applies. A memory is global, language
scoped (`lang:python`), or repository scoped (`repo:github.com/acme/api`).
The most specific applicable memory wins, and personal beats team for the
same slot. Retrieval is hybrid: BM25F over memory fields, embeddings, and
a developer-concept layer that knows "write tests" relates to testing
conventions. It's combined with importance, confidence, recency, scope
and reinforcement, explains every score, and is deterministic. The
context compiler packs a token budget with the task-relevant layer first
and the always-on profile (rules, conventions, tooling) second.

## Quickstart

Requires Python 3.11+.

```bash
pip install .                        # from a checkout (PyPI release pending); add [postgres] / [llm] as needed

mnemosyne init                      # creates ~/.mnemosyne (owner-only permissions)
mnemosyne learn repo ~/code/api     # learn a repository you work on (repeat for others)
mnemosyne learn sessions            # learn from your past Claude Code sessions
mnemosyne install claude-code       # from now on, every Claude Code session is briefed automatically
mnemosyne doctor                    # check everything is wired up
```

Try it without touching anything: `mnemosyne demo` briefs an assistant on
three tasks for a sample developer, in memory.

## Integrations

| Tool | How | What it does |
|---|---|---|
| **Claude Code** | `mnemosyne install claude-code` (hooks + MCP) | Profile at session start; relevant memories with each prompt; learns from each prompt live and from the transcript at compaction/end. |
| **Cursor, Windsurf, Claude Desktop, any MCP client** | `mnemosyne install cursor`, or run `mnemosyne mcp` (stdio) | Tools: `get_context`, `recall`, `remember`, `feedback`, `forget`, `profile`, `learn_repository`. |
| **Copilot, Gemini, Codex, Cursor rules, humans** | `mnemosyne sync [--all]` | Writes a marked block into `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`, `GEMINI.md`, and `.cursor/rules/mnemosyne.mdc`. Only repository and team knowledge by default, so it is safe to commit. |
| **Anything else** | REST API (`mnemosyne serve`, OpenAPI at `/docs`) | `POST /api/v1/context`, `/recall`, `/learn/*`, memories CRUD, export, and more. |

Hooks never break a session: failures are logged to
`~/.mnemosyne/logs/hooks.log` and the hook exits cleanly. Injected context
is capped below Claude Code's 10,000-character limit and wrapped in
`<mnemosyne-context>`, so it is never learned back from transcripts.
After the first hook of a session, a small background daemon answers
hooks in **~100 ms** instead of ~730 ms. See
[docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).

## Teams and companies

One developer's memory lives in a local SQLite database. A team runs a
Mnemosyne server (PostgreSQL, API keys) and points developer machines at
it:

```bash
POSTGRES_PASSWORD=$(openssl rand -hex 16) docker compose up -d
docker compose exec api mnemosyne admin user create alice --admin    # prints an API key, once
docker compose exec api mnemosyne admin team create payments
docker compose exec api mnemosyne admin team add payments alice --role maintainer
```

```bash
# on each developer machine, in ~/.mnemosyne/config.env
MNEMOSYNE_SERVER_URL=https://mnemosyne.internal.example.com
MNEMOSYNE_SERVER_API_KEY=mn_…
```

Every CLI command, hook and MCP tool then works against the server.
Repositories are still analysed on the developer's machine, and
transcripts are redacted there before anything is sent.

The server also provides:

- **Tenancy.** Personal memories are private. Team memories are shared
  with members; members contribute, and maintainers can also delete
  permanently. Anything you can't see reads as 404.
- **Access control.** Scoped, expiring, revocable API keys, stored as
  SHA-256 hashes.
- **Audit log** of every change.
- **Data rights.** Export, import and right-to-erasure endpoints.
- **Operations.** Per-key rate limits, request size limits, Prometheus
  `/metrics`, `/health` and `/ready` (schema revision).

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Privacy and security

- **Local-first.** By default nothing leaves the machine, and the only
  network calls are the ones you configure (team server, optional LLM
  extraction, optional embedding API).
- **Secrets are redacted before storage.** This covers provider tokens,
  credential assignments, URL passwords, private keys, and high-entropy
  strings. A statement that is mostly secret is not stored at all.
- **Configuration can't be hijacked by a repository.** It's never read
  from a `.env` in the working directory: hooks run inside whatever
  repository is open, and a cloned repository must not be able to
  redirect your memory.
- **The local server refuses unsafe binds.** It won't bind a network
  interface without authentication.
- **The hook daemon's socket is private.** Only the owner can use it, and
  the client checks ownership before sending a prompt.

Threat model and controls: [SECURITY.md](SECURITY.md).

## Measured quality

From the labeled evaluation persona (101 memories, 4 repositories, 41
in-sample + 15 held-out queries written before tuning), with the default
zero-dependency embedder:

| | Recall@5 | MRR | nDCG@5 | Cross-repo leakage |
|---|---|---|---|---|
| In-sample | 0.935 | 0.874 | 0.869 | 0 |
| Held-out | 0.900 | 0.806 | 0.817 | 0 |

The compiled context (what the assistant actually receives, 1,500-token
budget) contains **100%** of essential memories in-sample and **93%**
held-out.

| Performance (this development container) | |
|---|---|
| Context compile / recall, 100 memories | p95 ~3 ms |
| Recall, 10,000 memories | p50 20 ms, p95 25 ms |
| Claude Code hook, cold process / warm daemon | ~730 ms / ~100 ms (p50) |

Method, ablations and known limits: [docs/EVALUATION.md](docs/EVALUATION.md).
Reproduce with `mnemosyne eval`, `python benchmarks/scale.py`, and
`python benchmarks/hook_latency.py`.

## Command reference

| Command | Purpose |
|---|---|
| `init`, `status`, `doctor` | Set up, inspect, diagnose |
| `remember TEXT [--here \| --lang L] [-t TYPE]` | Tell it something directly |
| `recall QUERY [--explain]` | Search, ranked for the current repository |
| `context [TASK]`, `profile` | What an assistant would be told |
| `list`, `show ID`, `forget ID`, `feedback ID SIGNAL` | Inspect and correct (8-character ids are fine) |
| `learn repo [PATHS…]`, `learn sessions`, `learn session FILE`, `learn text` | Teach it |
| `sync [--all] [--audience personal]` | Write rule files |
| `export`, `import` | Portable JSON bundle |
| `install claude-code \| cursor`, `uninstall claude-code` | Wire up tools (merges settings; keeps a backup) |
| `serve`, `mcp`, `daemon status \| stop` | Servers |
| `admin user \| key \| team …`, `db upgrade \| current` | Administration |
| `demo`, `eval` | Try it; measure it |

## Configuration

All settings are environment variables prefixed `MNEMOSYNE_`. They can
also go in `~/.mnemosyne/config.env`, or in a file named by
`MNEMOSYNE_CONFIG_FILE`. The full list, with defaults, is in
[`src/mnemosyne/config.py`](src/mnemosyne/config.py), and
[`config.env.example`](config.env.example) covers the common ones.

## Development

```bash
pip install -e ".[dev]"
make check            # ruff + mypy + the test suite (411 tests; 3 run only with PostgreSQL)
make test-postgres    # also against PostgreSQL (needs MNEMOSYNE_TEST_POSTGRES_URL)
make eval bench       # retrieval quality and performance
```

| Document | |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Layers, data model, retrieval, learning, surfaces |
| [DECISIONS.md](DECISIONS.md) | Why things are the way they are |
| [SECURITY.md](SECURITY.md) | Threat model and controls |
| [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) | Claude Code, MCP clients, rule files, REST |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running a team server |
| [docs/EVALUATION.md](docs/EVALUATION.md) | How quality is measured, and the numbers |
| [ROADMAP.md](ROADMAP.md) | What's next |

## License

MIT
