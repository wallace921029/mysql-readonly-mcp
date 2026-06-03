# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **read-only, remote, multi-tenant** MCP (Model Context Protocol) server exposed over
Streamable HTTP. One process hosts multiple databases across multiple MySQL instances.
Callers are distinguished by Bearer token, and access is isolated at `source.database`
granularity. The point of the remote design: DB credentials stay centralized on the
server, while each client holds only a scope-limited bearer token that can be revoked
individually.

## Local development (no Docker)

```bash
uv sync
export CONFIG_PATH=./config.yaml          # PowerShell: $env:CONFIG_PATH = "./config.yaml"
set -a; source ./.env.secrets; set +a     # load DB passwords + tokens into env
uv run main.py                            # serves on http://0.0.0.0:8000/mcp
```

You need a `config.yaml` (copy `config.example.yaml`) and a `.env.secrets`
(copy `.env.secrets.example`). The server refuses to start if any env var named by
`password_env` / `token_env` is missing — config validation is fail-fast at load time.

Manual end-to-end testing uses the MCP Inspector (there is no automated test suite):

```bash
npx @modelcontextprotocol/inspector
# URL: http://localhost:8000/mcp   Header: Authorization: Bearer <token>
# Flow: list_sources -> list_tables -> describe_table -> run_query "SELECT 1"
```

Quick auth smoke check: `curl -i http://localhost:8000/mcp` should return `401` with a
`WWW-Authenticate` header; the same request with a valid `Authorization: Bearer <token>`
returns a 200/SSE stream.

## Build & deploy

Deployment is **build locally → `docker save` to tar → scp → `docker load` → run** (no
registry). The image is always tagged `latest`; `docker-compose.yml` has no `build:`
section and just runs the loaded image. Full step-by-step (including optional nginx+TLS)
is in `README.md`. The common loop:

```powershell
docker build -t mysql-readonly-mcp:latest .
docker save -o mysql-readonly-mcp.tar mysql-readonly-mcp:latest
# scp the tar to the server, then on the server: docker load -i ...; docker compose up -d
```

## Architecture

`main.py` loads config → `build_app()` assembles the ASGI app → `uvicorn.run`. The whole
server is small; the key flow is the layered request pipeline:

1. **`src/auth.py` — `BearerAuthMiddleware`** wraps the FastMCP ASGI app. Every HTTP
   request must carry `Authorization: Bearer <token>`. The token is matched in constant
   time against all configured tokens (padded `hmac.compare_digest`, loop runs to
   completion regardless of hit) and the resolved `TokenConfig` identity is stashed in the
   `current_identity` **contextvar** for the duration of the request. No token → `401`.

2. **`src/server.py` — FastMCP tools** (`list_sources`, `list_tables`, `describe_table`,
   `run_query`). Every tool that touches data calls `_resolve()`, which: validates the
   source/database names against `_IDENT` regex, pulls the identity from the contextvar via
   `require_identity()`, checks `identity.can_access(source, database)`, and confirms the
   database is actually exposed under that source. This is where per-token authorization
   lives — the contextvar is the bridge from middleware to tool handler.

3. **`src/validator.py` — `validate_read_only`** is the application-layer read-only gate
   for `run_query`. It strips comments, **rejects stacked queries** (any remaining `;`),
   requires a leading keyword in `{SELECT, SHOW, DESCRIBE, DESC, EXPLAIN, WITH}`, and
   rejects any token matching a forbidden-keyword set (INSERT/UPDATE/DROP/SET/INTO/...).
   Treat this as defense-in-depth on top of a least-privileged DB user, not the only line.

4. **`src/db.py` — `execute`** opens a fresh pymysql connection per call (no pool by
   design at this scale), sets `MAX_EXECUTION_TIME` server-side as a second timeout guard,
   runs the single statement, returns rows as dicts.

5. **`src/audit.py` — `AuditLogger`** appends one JSON line per `run_query` (success and
   failure) to `audit.log_path`, fsync'd under a lock. No-op when `log_path` is unset.

### Config model (`src/config.py`)

`config.yaml` defines topology + auth rules and **only references env var names** (so it's
safe to review/commit); real secrets come from `.env.secrets`. Parsed into frozen
dataclasses: `AppConfig` → `{ServerConfig, sources: {name: SourceConfig}, tokens: (TokenConfig...), AuditConfig}`.
`AppConfig._by_token` is the token→identity lookup the auth middleware uses.

`allow` rules have exactly three forms, enforced both at parse time (`_validate_rule`) and
query time (`TokenConfig.can_access`): `"*"` (everything), `"<source>.*"` (all DBs in a
source), `"<source>.<database>"` (one DB). Rules are validated against the actual
sources/databases at load time, so a typo'd rule fails startup rather than silently
denying.

## Conventions & gotchas

- **Identity flows through a contextvar, not function args.** Tool handlers get the caller
  via `require_identity()`; don't thread it manually. It's only set inside the middleware's
  request scope.
- **Two regexes guard injection of identifiers** since table/source/db names are
  interpolated into SQL (`DESCRIBE \`{table}\``, `SHOW TABLES`): `_IDENT`
  (`[A-Za-z0-9_-]+`) for source/database, `_TABLE_IDENT` (`[A-Za-z0-9_]+`) for table
  names. Keep new identifier interpolation behind a fullmatch check.
- **DNS-rebinding protection is intentionally disabled** in `build_app` (FastMCP would
  otherwise reject any non-localhost `Host` header). Bearer auth replaces that protection
  here — see the comment in `src/server.py`.
- **Row limits**: `run_query` defaults to 100 rows, hard cap 1000 (`DEFAULT_ROW_LIMIT` /
  `MAX_ROW_LIMIT` in `server.py`); excess is truncated and flagged `truncated: true`.
- Requires Python **3.13+**; dependencies managed with **uv** (`uv.lock` is committed,
  Docker build uses `--frozen`).
- `README.md` is the authoritative, detailed deployment/runbook reference (in Chinese).
