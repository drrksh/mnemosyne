# Roadmap

## Where it stands (0.2)

**Learns**
- Repository analysis:
  - code: Python (AST), TypeScript/JavaScript, Go;
  - manifests: Python, JavaScript/TypeScript, Go, Rust and Ruby;
  - configuration: CI, containers, infrastructure-as-code, Make/just,
    EditorConfig;
  - git history (the developer's own commits).
- Conversations: live from each prompt, transcripts at compaction or
  session end, backfill of past sessions.
- Optional LLM extraction with verbatim grounding.
- Explicit statements, feedback, imports.
- Consolidation with slots, scopes and origin authority.
- Cross-repository generalization.

**Delivers**
- Claude Code hooks, with a warm daemon at ~100 ms per hook.
- An MCP server.
- Rule files: CLAUDE.md, AGENTS.md, Copilot, Cursor, Gemini.
- REST API and CLI.

**Operates**
- Local-first SQLite, or a team server on PostgreSQL.
- API keys, teams and roles, tenancy enforced in queries.
- Audit log, export, import, erasure.
- Rate limits, Prometheus metrics, health and readiness.
- Docker image and Compose file; CI on Python 3.11–3.13 with PostgreSQL.

**Measured**
- Retrieval quality on a labeled persona with held-out queries.
- Context coverage; scale to 10,000 memories; hook latency.

## Next

| Item | Why |
|---|---|
| **Measure neural embedders** on the evaluation persona (`http`, `sentence-transformers`) | Implemented but unmeasured. They should help the open-domain synonymy cases the hashing embedder misses (docs/EVALUATION.md, "Known limits"). Ship a recommended default per deployment type. |
| **Publish to PyPI**, with `pipx install mnemosyne` and signed releases | Installation today is from a checkout. |
| **Usefulness feedback from real use** | Infer `helpful` / `not_helpful` from whether the assistant's output followed a served memory, or the developer corrected it, so confidence tracks reality without manual feedback. |
| **Staleness and decay** | Archive inferred memories whose evidence disappeared from the repository (the analyzer no longer sees the convention); flag memories unused for a long time for review. |
| **Review UI** | A small web view over the API to browse, confirm and correct memories, and for team maintainers to curate team memory. |
| **More analyzers** | Rust (beyond manifests), Java/Kotlin, C#, Ruby. Deeper TypeScript via a real parser. |
| **SSO for team servers** (OIDC), with keys issued per device from a login | API keys alone don't meet many companies' identity requirements. |
| **Windows hook daemon** (named pipes) | Hooks work on Windows today, without the speed-up. |

## Later

- **Organisation policies.** Memories that apply to every team (security
  rules, compliance requirements), owned by an org admin. Team and
  personal memories can't override them.
- **Relationships between memories** (`decision` → the `failure` that
  motivated it → the `convention` it produced), to explain *why* in
  context.
- **Encryption at rest per tenant**, for hosted multi-tenant deployments.
- **pgvector** for very large organisations, where an in-process index
  per principal stops being the right shape.
- **IDE plugins** (VS Code, JetBrains) that learn from accepted and
  rejected suggestions through `POST /api/v1/learn/candidates`.
- **Code review learning.** Review comments as evidence for team
  conventions.
