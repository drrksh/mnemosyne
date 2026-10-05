# Integrations

Mnemosyne reaches assistants four ways. They all go through the same
backend: the local database by default, or a team server when
`MNEMOSYNE_SERVER_URL` is set. So a memory learned through one surface is
recalled identically through the others.

| Surface | Push or pull | Best for |
|---|---|---|
| [Claude Code hooks](#claude-code) | Push, automatic | Claude Code: zero-effort context and learning |
| [MCP server](#mcp-clients) | Pull, by the assistant | Cursor, Windsurf, Claude Desktop, Claude Code, any MCP client |
| [Rule files](#rule-files) | Static | Copilot, Gemini, Codex and other tools that read `AGENTS.md`-style files; also human readers |
| [REST API](#rest-api) | Either | IDE plugins, internal tools, CI |

---

## Claude Code

```bash
mnemosyne install claude-code                 # every project (~/.claude/settings.json) + MCP
mnemosyne install claude-code --scope project # this repository, shared via .claude/settings.json + .mcp.json
mnemosyne install claude-code --scope local   # this repository, just you (.claude/settings.local.json)
mnemosyne install claude-code --dry-run       # show what would change
mnemosyne uninstall claude-code               # remove the hooks (memory is kept)
```

The installer merges into the existing settings file. Other hooks and
settings are untouched, a timestamped backup is written next to it, and
running it again changes nothing. It refuses to edit a settings file it
cannot parse.

### What each hook does

| Event | Mnemosyne | Output |
|---|---|---|
| `SessionStart` (startup, resume, clear, compact, fork) | Compiles the developer's profile for the current repository: rules, preferences, conventions, tooling, how to run things. | `additionalContext`, up to 1,800 tokens / 9,000 characters |
| `UserPromptSubmit` | 1. Learns from the prompt immediately ("no, use pnpm instead of npm") and says so. 2. Adds memories relevant to *this* prompt, only if there are any. | `additionalContext`, or nothing |
| `PreCompact`, `SessionEnd` | Starts a detached background process that learns from the whole transcript (user turns only), so the hook returns at once. | nothing |

Injected text is wrapped in `<mnemosyne-context>` tags, which the
transcript parser strips. Mnemosyne therefore never learns from its own
output.

Hooks cannot break a session. Every error is caught, logged to
`~/.mnemosyne/logs/hooks.log`, and the hook exits 0 with no output.

### Latency and the hook daemon

Claude Code starts a new process for every hook. Importing the database
and retrieval stack takes most of a second; the work itself takes tens of
milliseconds. So the first hook of a session answers in-process and
starts a per-user background daemon, and later hooks hand their event to
it over a private Unix socket:

| | p50 | p95 |
|---|---|---|
| Cold process | ~730 ms | ~800 ms |
| Warm daemon | ~100 ms | ~110 ms |

These figures come from `python benchmarks/hook_latency.py` (100-memory
profile), which also checks the two paths produce byte-identical output.

The daemon is disposable. It exits after 30 idle minutes
(`MNEMOSYNE_HOOK_DAEMON_IDLE_MINUTES`), when `~/.mnemosyne/config.env`
changes, or when another version is installed. If it can't be reached,
hooks fall back to in-process handling.

```bash
mnemosyne daemon status
mnemosyne daemon stop          # e.g. after changing environment variables
MNEMOSYNE_HOOK_DAEMON=false    # turn it off (Windows has no daemon; hooks work, just slower)
```

### Learning from past sessions

```bash
mnemosyne learn sessions                 # ~/.claude/projects, last 90 days
mnemosyne learn sessions --since 365 --llm
```

Sessions already learned from are remembered, so running this again only
reads new or grown transcripts.

### Optional: LLM extraction

The built-in heuristics are precision-first: they only store statements
that are clearly standing preferences. To also catch subtler ones
("hmm, I'd rather keep these as plain functions"), enable LLM extraction:

```bash
pip install "mnemosyne[llm]"
echo "MNEMOSYNE_LLM_EXTRACTION=true" >> ~/.mnemosyne/config.env   # uses ANTHROPIC_API_KEY
```

Transcripts are redacted before they are sent. The model must quote the
user verbatim for each finding; findings whose quote is not in a user
turn are discarded.

### Troubleshooting

```bash
mnemosyne doctor                        # hooks installed? store reachable? daemon? recent hook failures?
tail ~/.mnemosyne/logs/hooks.log        # one line per hook: name, duration, what happened
echo '{"cwd": "'$PWD'"}' | mnemosyne hook session-start     # see exactly what Claude Code receives
```

---

## MCP clients

`mnemosyne mcp` runs a Model Context Protocol server on stdio.

| Tool | Purpose |
|---|---|
| `get_context(task, cwd?, file_path?, max_tokens?)` | Briefing for a task: relevant memories plus the profile |
| `recall(query, cwd?, limit?)` | Search; results include ids |
| `remember(statement, kind?, applies_to?, language?, cwd?)` | Store a lasting preference, convention or decision. `applies_to`: `auto`, `everywhere`, `this_repository`, `language` |
| `feedback(memory_id, signal, note?)` | `helpful`, `not_helpful`, `confirm`, `wrong`, `outdated` |
| `forget(memory_id)` | Archive a memory |
| `profile(cwd?)` | The standing profile for a repository |
| `learn_repository(path?)` | Analyse a local repository |

There is also a resource, `mnemosyne://profile`. The server sends
instructions telling the assistant when to use each tool. Tools are
annotated read-only or destructive, so clients can apply their own
confirmation policies.

`cwd` defaults to the server process's working directory. Claude Code and
Cursor start project-scoped servers in the project directory. For
clients without a project (Claude Desktop), the assistant should pass
`cwd`.

### Claude Code

`mnemosyne install claude-code` registers it. Manually:

```bash
claude mcp add --scope user mnemosyne -- mnemosyne mcp
```

### Cursor

```bash
mnemosyne install cursor                       # ~/.cursor/mcp.json
mnemosyne install cursor --scope project       # .cursor/mcp.json + .cursor/rules/mnemosyne.mdc
```

### Claude Desktop, Windsurf, others

Add to the client's MCP configuration (for Claude Desktop,
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mnemosyne": { "command": "mnemosyne", "args": ["mcp"] }
  }
}
```

Use the absolute path from `which mnemosyne` if the client does not
inherit your shell's `PATH`.

---

## Rule files

```bash
mnemosyne sync                       # CLAUDE.md and AGENTS.md
mnemosyne sync --all                 # + .github/copilot-instructions.md, GEMINI.md, .cursor/rules/mnemosyne.mdc
mnemosyne sync --target copilot      # just one
mnemosyne sync --audience personal   # include personal preferences (for files you don't commit)
```

Mnemosyne owns a block marked `<!-- mnemosyne:begin … -->` …
`<!-- mnemosyne:end -->` inside shared files and never touches the rest.
It owns `.cursor/rules/mnemosyne.mdc` entirely.

By default only **repository and team** knowledge is written, because
these files are usually committed, and one developer's personal
preferences ("keep answers terse") shouldn't be imposed on teammates.
Personal context keeps flowing through hooks and MCP.

Run `sync` from a scheduled CI job or a pre-commit hook to keep the files
current. Against a team server, `sync` uses the server's knowledge
(`GET /api/v1/rules`).

---

## REST API

`mnemosyne serve` (local, loopback, no authentication) or a team server
(`Authorization: Bearer mn_…`). Interactive OpenAPI documentation is at
`/docs`.

```bash
H='Authorization: Bearer mn_…'

# A briefing for a task in a repository
curl -s -X POST $URL/api/v1/context -H "$H" -H 'Content-Type: application/json' -d '{
  "task": "add a refund endpoint",
  "context": {"repo_id": "github.com/acme/payments", "languages": ["python"]},
  "max_tokens": 1200, "format": "markdown", "explain": true
}'

# Search with score breakdowns
curl -s -X POST $URL/api/v1/recall -H "$H" -H 'Content-Type: application/json' \
  -d '{"query": "how are amounts stored", "context": {"repo_id": "github.com/acme/payments"}, "explain": true}'

# Teach it from a conversation (redact on your side first if you can)
curl -s -X POST $URL/api/v1/learn/conversation -H "$H" -H 'Content-Type: application/json' \
  -d '{"messages": [{"role": "user", "content": "From now on, never mock the database in integration tests."}]}'

# Correct it
curl -s -X POST $URL/api/v1/memories/$ID/feedback -H "$H" -H 'Content-Type: application/json' \
  -d '{"signal": "outdated", "note": "we moved to Postgres 16"}'
```

### Building an integration (IDE plugin, internal tool)

Send what you observed as candidates; Mnemosyne consolidates them with
everything else. Re-sending the same `source_ref` is a no-op, so
extractors can safely re-run.

```bash
curl -s -X POST $URL/api/v1/learn/candidates -H "$H" -H 'Content-Type: application/json' -d '{
  "source": "acme-ide-plugin",
  "candidates": [{
    "title": "Prefers early returns over nested conditionals",
    "memory_type": "pattern", "origin": "inferred", "confidence": 0.8,
    "scope": "lang:python",
    "evidence": [{"kind": "code", "source_ref": "ide:acme/payments@3f2a1c:refunds.py#L40", "excerpt": "if not order: return"}]
  }]
}'
```

The response reports what happened to each candidate: `created`,
`reinforced`, `superseded` (with the old and new value), `kept_existing`
(a more authoritative memory disagrees), or `skipped` (with the reason).

| Endpoint | |
|---|---|
| `POST /api/v1/context`, `GET /api/v1/profile`, `GET /api/v1/rules` | Context for assistants and rule files |
| `POST /api/v1/recall` | Search |
| `POST /api/v1/learn/candidates`, `/learn/conversation`, `/learn/generalize`; `/learn/repository` (local mode only) | Learning |
| `GET/POST/PATCH/DELETE /api/v1/memories…`, `/memories/{id}/versions`, `/evidence`, `/feedback`, `/archive`, `/restore` | Memories |
| `GET /api/v1/me`, `/me/stats`, `/me/export`; `POST /me/import`; `DELETE /me/memories?confirm=…`; `/me/keys` | Account and data rights |
| `GET /api/v1/audit`; `/api/v1/admin/users`, `/keys`, `/teams` | Audit and administration |
| `GET /health`, `/ready`, `/metrics` | Operations |
