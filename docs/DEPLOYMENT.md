# Running a team server

A team server is the same Mnemosyne running behind an API, with
PostgreSQL, API keys and shared team memory. Developer machines keep
running the CLI, hooks and MCP server, pointed at it.

```
 developer machines                                     server
 ──────────────────                                     ──────
 mnemosyne CLI / hooks / MCP ── HTTPS + API key ──► reverse proxy (TLS)
   • analyses repositories locally                         │
   • redacts transcripts locally                    mnemosyne serve ──► PostgreSQL
```

## 1. Start it

With Docker Compose (from a checkout of this repository):

```bash
export POSTGRES_PASSWORD=$(openssl rand -hex 16)      # keep it somewhere safe
docker compose up -d
curl -s localhost:8420/ready      # {"ready": true, "schema_revision": "…", …}
```

The image runs as an unprivileged user (uid 10001). It defaults to
`MNEMOSYNE_AUTH_MODE=api_key`, applies migrations at start-up, and has a
health check. Without Docker:

```bash
pip install ".[postgres]"
export MNEMOSYNE_DATABASE_URL=postgresql+psycopg://mnemosyne:…@db.internal:5432/mnemosyne
export MNEMOSYNE_AUTH_MODE=api_key MNEMOSYNE_API_HOST=0.0.0.0
mnemosyne db upgrade
mnemosyne serve --workers 4
```

## 2. Create users, keys and teams

From the server, before any API key exists:

```bash
docker compose exec api mnemosyne admin user create alice --admin     # prints alice's API key, once
docker compose exec api mnemosyne admin user create bob               # a regular user, with a key
docker compose exec api mnemosyne admin team create payments
docker compose exec api mnemosyne admin team add payments alice --role maintainer
docker compose exec api mnemosyne admin team add payments bob
docker compose exec api mnemosyne admin key issue bob --name laptop --expires-in-days 90
docker compose exec api mnemosyne admin key revoke key_…
docker compose exec api mnemosyne admin user disable bob               # all of bob's keys stop working
```

The same operations are available over HTTP to admin keys under
`/api/v1/admin/…`. Users manage their own keys at `/api/v1/me/keys`.
Command-line administration is audited as `cli:<os-account>`.

| Role | Can |
|---|---|
| user | Read and write their own memories; read and contribute to their teams' memories |
| team maintainer | …and permanently delete that team's memories |
| admin | Manage users, keys and teams; read metrics |

Key scopes are `memories:read` and `memories:write` (the default), plus
`admin`, which needs an admin user. A read-only key is useful for CI jobs
that only run `mnemosyne sync`.

## 3. Connect developer machines

Each developer puts their key in `~/.mnemosyne/config.env` (keep it
`chmod 600`):

```bash
MNEMOSYNE_SERVER_URL=https://mnemosyne.internal.example.com
MNEMOSYNE_SERVER_API_KEY=mn_…
```

Then they run `mnemosyne init` (which checks the connection) and
`mnemosyne install claude-code` as usual. Nothing is stored locally
except logs.

To bring an existing local memory to the server, run `mnemosyne export -o
mine.json` before setting the server URL, and `mnemosyne import mine.json`
after.

## Configuration

Settings are environment variables. You can also put them in a file and
name it with `MNEMOSYNE_CONFIG_FILE`. See
[`config.env.example`](../config.env.example) and
[`src/mnemosyne/config.py`](../src/mnemosyne/config.py).

| Setting | Default | |
|---|---|---|
| `MNEMOSYNE_DATABASE_URL` | SQLite under `MNEMOSYNE_HOME` | `postgresql+psycopg://…` for servers |
| `MNEMOSYNE_AUTH_MODE` | `local` (image: `api_key`) | `local` refuses non-loopback binds |
| `MNEMOSYNE_API_HOST` / `_PORT` | `127.0.0.1` / `8420` (image: `0.0.0.0`) | |
| `MNEMOSYNE_AUTO_MIGRATE` | `true` | Set `false` with several replicas; run `mnemosyne db upgrade` as a release step |
| `MNEMOSYNE_RATE_LIMIT_PER_MINUTE` | `600` | Per API key, per process; `0` disables |
| `MNEMOSYNE_MAX_REQUEST_BYTES` | 10 MB | Transcripts can be large |
| `MNEMOSYNE_METRICS_PUBLIC` | `false` | `/metrics` needs an admin key unless true |
| `MNEMOSYNE_CORS_ORIGINS` | none | JSON list, for browser clients |
| `MNEMOSYNE_LOG_FORMAT` | `json` | Structured logs with request ids |
| `MNEMOSYNE_LLM_EXTRACTION` | `false` | Server-side LLM extraction for `/learn/conversation` with `use_llm` |

## TLS

The server speaks plain HTTP. Put it behind a reverse proxy or load
balancer that terminates TLS. With Caddy, for example:

```
mnemosyne.internal.example.com {
    reverse_proxy 127.0.0.1:8420
}
```

## Upgrades

1. Back up the database.
2. Deploy the new version. With one replica and `MNEMOSYNE_AUTO_MIGRATE=true`,
   migrations run at start-up. Otherwise run `mnemosyne db upgrade` once
   before switching traffic.
3. `GET /ready` returns 503 until the schema matches what the running
   version expects, so a load balancer won't route to a replica with a
   stale schema.

Clients and servers should run the same minor version.

## Backups and data rights

- **Back up the database** with the usual PostgreSQL tooling (`pg_dump`,
  managed snapshots). The database is the only stateful component.
- **Per-user export:** `GET /api/v1/me/export` (or `mnemosyne export`)
  returns a portable bundle; `POST /api/v1/me/import` re-imports it, and
  is safe to repeat.
- **Erasure:** `DELETE
  /api/v1/me/memories?confirm=ERASE-ALL-MY-PERSONAL-MEMORIES` permanently
  deletes a user's personal memories with their history, evidence and
  embeddings. Team memories they contributed stay with the team.

## Monitoring

| Endpoint | |
|---|---|
| `GET /health` | Liveness: the process is up and the database answers |
| `GET /ready` | Readiness: schema at the expected revision (503 otherwise) |
| `GET /metrics` | Prometheus: `mnemosyne_http_requests_total{method,route,status}`, `mnemosyne_http_request_duration_seconds` (histogram), `mnemosyne_rate_limited_total`, `mnemosyne_uptime_seconds` |

Every response carries `X-Request-ID` (or echoes the caller's), and log
lines include it.

## Scaling

One process comfortably serves a team: context compilation and recall
take milliseconds, and recall over 10,000 memories is about 20 ms. For
more, run several workers (`mnemosyne serve --workers N`) or replicas.
They share nothing but PostgreSQL. Each process keeps its own retrieval
index cache, validated against the database on every request, so writes
made through one replica are seen by all.

Rate limits and metrics are per process. Limit at the gateway as well,
and scrape every replica.

Use SQLite only for single-process deployments.

## Security checklist

- [ ] `MNEMOSYNE_AUTH_MODE=api_key`
- [ ] TLS in front of the server
- [ ] A strong `POSTGRES_PASSWORD`; the database not exposed publicly
- [ ] Encrypted storage for the database volume
- [ ] Keys issued per person and per machine, with expiry; revoke on departure (`admin user disable`)
- [ ] `/metrics` scraped with an admin key, or kept on a private network with `MNEMOSYNE_METRICS_PUBLIC=true`
- [ ] Backups tested

See [SECURITY.md](../SECURITY.md) for the threat model.
