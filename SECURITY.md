# Security

Mnemosyne stores how people and teams build software, and feeds that
knowledge into AI assistants. That makes two things critical. Memory
contents must stay confidential, including from other users of the same
server. And no outside party may be able to plant "memories" that steer
an assistant. This document states the threat model and the controls.
Each control is implemented in the code and covered by a test.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the maintainers, via
the repository's security advisory feature, rather than in a public
issue. Include a reproduction if you can. We aim to acknowledge reports
within three working days.

## Deployment models

| Model | Who can reach the data | Authentication |
|---|---|---|
| **Local** (default) | The developer's OS account: `~/.mnemosyne` is created with mode `0700`. | None needed. The HTTP server refuses to bind anything but loopback in this mode (`check_bind_safety`). |
| **Team server** | Holders of API keys. PostgreSQL behind it. | `Authorization: Bearer mn_…` on every request (`MNEMOSYNE_AUTH_MODE=api_key`). |

## Threats and controls

### 1. Secrets ending up in memory

A developer pastes a token into a prompt; a config file holds a password.

- Everything is redacted before storage (`core/redaction.py`). This covers
  provider tokens (GitHub, AWS, Anthropic, OpenAI, Slack, Stripe, Google
  and others), credential assignments (`password = …`, `api_key: …`,
  with a plausibility check so prose and type hints aren't mangled), URL
  passwords, PEM private keys, and high-entropy strings.
- A statement that is *mostly* secret is not stored at all, not even
  redacted.
- **In team mode, transcripts are redacted on the developer's machine**
  before they are sent to the server (`RemoteBackend.learn_conversation`).
- Text sent to the optional LLM extractor is redacted first.
- Evidence excerpts are truncated and redacted like everything else.

### 2. Planted memories (prompt injection through the memory)

An attacker gets text into a repository, a web page, or a tool output and
hopes it becomes a "convention" that later steers an assistant.

- **Only the developer's own words are mined from conversations.**
  Assistant turns, tool results and injected `<mnemosyne-context>` /
  system-reminder blocks are excluded (`learning/conversation/transcript.py`).
  So a file or web page an assistant reads cannot become a memory.
- **The LLM extractor must quote the user verbatim.** If the quote does
  not appear in a user turn, the finding is discarded (grounding check in
  `learning/conversation/llm.py`).
- **Repository analysis produces structured findings, not free text.**
  That means tool names from a fixed vocabulary, statistics such as "88%
  of functions are type-annotated", and run commands built from
  allow-listed script names (`pnpm test`, `make lint`), never script
  bodies. Repository files such as an existing `CLAUDE.md` are not read
  as instructions.
- **Learned repository knowledge is scoped to that repository.** It only
  reaches sessions in the same repository, where its source is already
  present. Cross-repository generalization only promotes vocabulary-keyed
  slot values agreed across several repositories the developer actually
  commits to.
- **Explicit statements outrank inferred ones**, and every memory carries
  its evidence and origin (`mnemosyne show <id>`), so a surprising memory
  can be traced and corrected (`feedback … wrong`).

### 3. Malicious repository on a developer's machine

Hooks and the MCP server run inside whatever repository is open, including
freshly cloned, untrusted ones.

- **Configuration is never read from the working directory.** A `.env` in
  a repository cannot set `MNEMOSYNE_SERVER_URL` and exfiltrate the
  developer's memory. Settings come only from the environment,
  `~/.mnemosyne/config.env`, or an explicitly named
  `MNEMOSYNE_CONFIG_FILE` (`config.py`; regression test
  `tests/unit/test_config_sources.py`).
- **Analysis never executes repository code.** Python is parsed with
  `ast`/`tokenize`, other languages with bounded regular expressions, and
  manifests with TOML/JSON parsers. `git` is invoked with fixed arguments
  and no shell. File count, file size and commit count are capped
  (`MNEMOSYNE_REPO_MAX_*`, `MNEMOSYNE_GIT_MAX_COMMITS`).

### 4. Cross-tenant access on a team server

- **Visibility is enforced in the repository layer.** Every query is
  filtered to the caller's own memories and their teams'
  (`visibility_clause`), not filtered after loading. Memories a caller
  can't see return **404**, not 403, so their existence isn't revealed.
- **Writes are permission-checked.** A user can create and edit team
  memories only for teams they belong to. Permanently deleting a team
  memory needs that team's maintainer role, or an admin.
- **API keys:** `mn_<prefix>_<secret>`, shown once, stored as SHA-256.
  - Lookup is by prefix, with constant-time comparison. An unknown prefix
    is compared against a dummy hash, so timing does not reveal which
    prefixes exist.
  - Keys carry scopes (`memories:read`, `memories:write`, `admin`) and an
    optional expiry, and can be revoked.
  - A key can't be issued with more permissions than its issuer holds.
    `admin` requires an admin user.
  - Disabling a user disables their keys immediately.
- **Server-side repository analysis is refused in `api_key` mode**, so
  clients can't make the server read its own filesystem (`repo_path` →
  403).
- **Limits:** per-key rate limiting (token bucket, `429` with
  `Retry-After`), and a request-size cap (`413`).
- **Metrics:** `/metrics` requires an admin key unless explicitly made
  public.

### 5. Local exposure on the developer's machine

- `~/.mnemosyne` and its `cache` and `logs` directories are `0700`.
- **The hook daemon listens on a Unix socket only.** The socket is `0600`
  inside a `0700` directory. When the home path is too long for a socket,
  a per-user directory under `$XDG_RUNTIME_DIR` or the temp directory is
  used.
- **The hook client checks before it sends anything.** It refuses to talk
  to a socket, or a socket directory, that is not owned by the user and
  private. Otherwise another local user could pre-create the directory in
  a shared `/tmp` and receive prompts.
- **Index snapshots are loaded through an allow-listed unpickler**, so a
  tampered cache file cannot instantiate arbitrary objects.

### 6. Accountability and data rights

- **Audit.** Every write (create, update, archive, delete, ingest,
  feedback, import, erasure, key issue and revocation, user and team
  changes) is recorded with its actor in the audit log (`GET
  /api/v1/audit`). Administration from the server's command line is
  recorded as `cli:<os-account>`.
- **Export.** `GET /api/v1/me/export` and `mnemosyne export` produce a
  portable bundle.
- **Right to erasure.** `DELETE /api/v1/me/memories` requires an explicit
  confirmation token and permanently deletes the caller's personal
  memories with their versions, evidence and embeddings.

## Third parties

By default Mnemosyne makes no network calls. It contacts other services
only when configured to:

- a team server (`MNEMOSYNE_SERVER_URL`);
- the Anthropic API for optional LLM extraction (`MNEMOSYNE_LLM_EXTRACTION`),
  sending redacted user turns only;
- an embeddings endpoint (`MNEMOSYNE_EMBEDDER=http`), sending memory text.

## Known limitations

- **No encryption at rest by Mnemosyne itself.** Rely on disk or database
  encryption: FileVault/LUKS locally, encrypted volumes or managed
  PostgreSQL on servers.
- **TLS is not terminated by the server.** Run it behind a reverse proxy
  or load balancer that does TLS for anything but a trusted network.
- **Rate limits and metrics are per process.** With several workers or
  replicas, also limit at the gateway.
- **Redaction is pattern-based.** It catches common credential formats
  and high-entropy strings; a secret in an unusual format written as
  prose can get through. Review `mnemosyne list` and use `forget
  --permanent` when in doubt.
