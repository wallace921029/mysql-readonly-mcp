> **Language / 语言**: **English** | [中文](README.zh-CN.md)

# mysql-readonly-mcp

A **read-only, remote, multi-tenant** MCP (Model Context Protocol) server, exposed over
the Streamable HTTP transport to any MCP client (Claude Desktop / OpenClaw / ...). A
single process can host multiple databases across multiple MySQL instances, distinguish
users by Bearer token, and isolate access at `source.database` granularity.

## Why remote

A stdio deployment puts DB credentials in a local `.env` on the client machine. On agent
platforms that have full local file access, those credentials can be read out-of-band.
Going remote instead:

- Keeps DB credentials centralized on the server; the client holds only a
  **scope-limited** bearer token.
- Bounds the blast radius even if a token leaks — application-layer read-only +
  row-count cap + 30s timeout act as backstops.
- Lets many people share one endpoint, each with their own token, revocable individually.
- Means server-side updates require no client config changes.

## Tools

| Tool | Description |
|------|-------------|
| `list_sources` | List the `(source, database)` pairs the current token can access |
| `list_tables(source, database)` | List all table names in a database |
| `describe_table(source, database, table_name)` | Table schema |
| `run_query(source, database, sql, row_limit=100)` | Run a read-only SQL query, return result rows |

## Security limits

- **Read-only**: only statements beginning with `SELECT/SHOW/DESCRIBE/EXPLAIN/WITH` are
  allowed; requests containing write/DDL keywords or multiple statements (stacked
  queries) are rejected.
- **Row cap**: `run_query` defaults to 100 rows, capped at 1000.
- **Timeout**: 30s per query, with MySQL `MAX_EXECUTION_TIME` as a server-side backstop.
- **Audit**: every `run_query` appends one JSON line to `audit.log_path`
  (who / what / rows / ok).
- Strongly recommended: use a **read-only account** (granted `SELECT` only) for each
  source, adding a second line of defense at the DB layer.

## Configuration

Two files:

- `config.yaml`: topology + auth rules. **Only references** env variable names, so it's
  safe to review/commit.
- `.env.secrets`: actual passwords + token values. **Must not be committed.**

See `config.example.yaml` / `.env.secrets.example`:

```yaml
sources:
  mysql-1:
    host: 10.0.0.5
    user: ro_user
    password_env: MYSQL1_PASSWORD
    databases: [orders, users]
  mysql-2:
    host: 10.0.0.9
    user: ro_user
    password_env: MYSQL2_PASSWORD
    databases: [analytics, logs, events]

tokens:
  - label: alice
    token_env: TOKEN_ALICE
    allow: [mysql-1.orders, mysql-1.users]
  - label: bob
    token_env: TOKEN_BOB
    allow: [mysql-2.*]
  - label: admin
    token_env: TOKEN_ADMIN
    allow: ["*"]
```

`allow` rules come in three forms: `source.database`, `source.*`, `*`.

Generate a token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Deployment

Workflow: **build the image locally → `docker save` to a tar → scp to the server →
`docker load` → run the container**. The project is not published to Docker Hub; this
loop is repeated on every update.

The container binds directly to `0.0.0.0:8000`, so after deployment the service is at
`http://<server-IP>:8000/mcp`. For HTTPS / a domain name, put an nginx + TLS layer in
front (see "Optional: nginx + HTTPS" at the end).

### Prerequisites

- **Local (Windows + Docker Desktop)**: can run `docker build` and `scp` (PowerShell
  ships with `scp`, or use WSL).
- **Server (Ubuntu 24.04)**: Docker installed.
- **Firewall / security group**: open **8000** (or **80/443** if fronted by nginx, in
  which case 8000 can stay internal-only).

---

### Phase 1 · Build and package the image locally

In the repo root (Windows, PowerShell):

```powershell
# 1) Build the image (fixed tag latest; compose hard-codes latest too)
docker build -t mysql-readonly-mcp:latest .

# 2) Export to a tar
docker save -o mysql-readonly-mcp.tar mysql-readonly-mcp:latest

# 3) Check the file size (~150-250 MB is normal)
Get-Item mysql-readonly-mcp.tar | Select-Object Name, Length
```

> **Cross-architecture note**: skip this if both local and server are x86_64. If your
> local machine is Apple Silicon (M1/M2/...), force amd64:
> `docker build --platform linux/amd64 -t mysql-readonly-mcp:latest .`

---

### Phase 2 · Transfer files to the server

For the first deployment, transfer the tar + compose + config templates; for
image-only updates afterward, transfer just the tar.

Assuming the target path on the server is `/srv/mcp` and the remote user is `ubuntu`:

```powershell
# Create the directory on the server first
ssh ubuntu@<server-IP> "sudo mkdir -p /srv/mcp/logs && sudo chown -R ubuntu:ubuntu /srv/mcp"

# scp the image + deployment files up in one go
scp mysql-readonly-mcp.tar `
    docker-compose.yml `
    config.example.yaml `
    .env.secrets.example `
    ubuntu@<server-IP>:/srv/mcp/
```

---

### Phase 3 · Load and start on the server

SSH in, `cd /srv/mcp`, then:

```bash
# 1) Load the image tar into the local Docker
docker load -i mysql-readonly-mcp.tar
docker images | grep mysql-readonly-mcp        # confirm mysql-readonly-mcp:latest is present

# 2) Prepare config files
cp config.example.yaml config.yaml             # edit source topology + token rules
nano config.yaml

cp .env.secrets.example .env.secrets           # fill in DB passwords + generate a token per user
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # run multiple times, one per user
nano .env.secrets
chmod 600 .env.secrets                         # tighten permissions

# 3) Start the container (no build; compose has no build: section, uses the loaded image)
docker compose up -d
docker compose ps                              # should show mcp as running

# 4) Self-check
curl -i http://127.0.0.1:8000/mcp              # expect 401 + WWW-Authenticate
```

If step 4 doesn't return 401, check the logs: `docker compose logs mcp`.

---

### Phase 4 · End-to-end verification

```bash
curl -i http://<server-IP>:8000/mcp                                       # 401 (no token)
curl -i -H "Authorization: Bearer <some-token>" http://<server-IP>:8000/mcp # 200 / SSE data
```

Run the full flow locally with MCP Inspector:

```powershell
npx @modelcontextprotocol/inspector
# URL: http://<server-IP>:8000/mcp
# Header: Authorization: Bearer <your token>
# In order: list_sources → list_tables → run_query "SELECT 1"
```

---

### Ongoing operations

**Code-only update (bug fix or new feature)**:
```powershell
# Local: after editing code → repackage (tag is always latest)
docker build -t mysql-readonly-mcp:latest .
docker save -o mysql-readonly-mcp.tar mysql-readonly-mcp:latest
scp mysql-readonly-mcp.tar ubuntu@<server-IP>:/srv/mcp/
```
```bash
# Server
cd /srv/mcp
docker load -i mysql-readonly-mcp.tar
docker compose up -d              # detects the changed image, recreates the container
docker image prune -f             # clean up the overwritten old latest (dangling image)
```

**Config-only change (add/remove/edit users or databases)**:
```bash
# Server
nano /srv/mcp/config.yaml          # and/or .env.secrets
docker compose -f /srv/mcp/docker-compose.yml restart mcp
```

**Audit / troubleshooting**:
```bash
tail -f /srv/mcp/logs/audit.log                          # one JSON line per run_query
docker compose -f /srv/mcp/docker-compose.yml logs -f mcp # runtime logs
```

---

### Optional: nginx + HTTPS

For a domain + HTTPS, use nginx on the host to reverse-proxy to `127.0.0.1:8000` (the
repo ships `nginx-mcp.conf.example`, with buffering/timeouts already tuned for
streamable-http):

```bash
# DNS: point an A record for mcp.yourdomain.com at the server's public IP
sudo cp nginx-mcp.conf.example /etc/nginx/sites-available/mcp.conf
sudo sed -i 's/mcp.yourdomain.com/<your-real-domain>/g' /etc/nginx/sites-available/mcp.conf
sudo ln -s /etc/nginx/sites-available/mcp.conf /etc/nginx/sites-enabled/
sudo certbot --nginx -d <your-real-domain>     # auto-issues the cert, rewrites ssl_* lines, auto-renews
sudo nginx -t && sudo systemctl reload nginx
```

After that, clients use the URL `https://<your-real-domain>/mcp`.

> Once nginx is in front, if you don't want port 8000 exposed to the public, change the
> `ports` in `docker-compose.yml` to `"127.0.0.1:8000:8000"`, leaving nginx as the only
> entry point.
>
> Key points in the nginx template: `proxy_buffering off` / `proxy_request_buffering off`
> (streamable-http is a streaming response; buffering stalls it), `proxy_read_timeout 600s`
> (SSE connections stay open a long time), `proxy_http_version 1.1` + `Connection ""`
> (keep-alive reuse).

## Client configuration (OpenClaw)

```json
{
  "mcp": {
    "servers": {
      "mysql-readonly": {
        "url": "http://<server-IP>:8000/mcp",
        "transport": "streamable-http",
        "headers": {
          "Authorization": "Bearer <your token>"
        }
      }
    }
  }
}
```

After nginx + HTTPS, swap `url` for `https://<your-real-domain>/mcp`; everything else
stays the same.

> `transport: "streamable-http"` must be declared explicitly — without it, OpenClaw
> defaults to SSE.

## Project structure

```
.
├── main.py                       # entrypoint: load config → build app → uvicorn.run
├── src/
│   ├── config.py                 # YAML + env parsing and validation
│   ├── validator.py              # read-only SQL validation
│   ├── db.py                     # pymysql connection & execution
│   ├── auth.py                   # Bearer token ASGI middleware
│   ├── audit.py                  # JSON-line audit log
│   └── server.py                 # FastMCP tools + ASGI assembly
├── config.example.yaml
├── .env.secrets.example
├── Dockerfile
├── docker-compose.yml             # MCP container, binds 0.0.0.0:8000
└── nginx-mcp.conf.example         # optional: host nginx server block example (HTTPS/domain)
```

## Local development (no Docker)

```bash
uv sync
export CONFIG_PATH=./config.yaml
set -a; source ./.env.secrets; set +a
uv run main.py
```
