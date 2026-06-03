# mysql-readonly-mcp

一个**只读、远程、多租户**的 MCP (Model Context Protocol) server，用 Streamable HTTP
transport 暴露给任意 MCP client（Claude Desktop / OpenClaw / ...）。一个进程可以
托管多个 MySQL 实例下的多个 database，按 Bearer token 区分用户、按
`source.database` 粒度做权限隔离。

## 为什么是远程

stdio 部署把 DB 凭据放在执行 client 的本机 `.env` 里。在那些拥有本机全文件
访问权限的 agent 平台上，凭据可被旁路读取。改成远程后：

- DB 凭据集中在云端服务器，本地只有一个**作用域受限**的 bearer token。
- 应用层只读 + 行数上限 + 30s 超时为兜底，token 即使泄漏，爆炸半径也有限。
- 多人共用同一 endpoint，每人一个 token，可单独撤销。
- 云端更新无需客户端改配置。

## 工具

| 工具 | 说明 |
|------|------|
| `list_sources` | 列出当前 token 能访问的 `(source, database)` 列表 |
| `list_tables(source, database)` | 列出库下所有表名 |
| `describe_table(source, database, table_name)` | 表结构 |
| `run_query(source, database, sql, row_limit=100)` | 执行只读 SQL，返回结果行 |

## 安全限制

- **只读**：仅放行 `SELECT/SHOW/DESCRIBE/EXPLAIN/WITH` 开头的语句；含写/DDL
  关键字或多条语句（堆叠查询）的请求被拒。
- **行数上限**：`run_query` 默认 100 行，最多 1000 行。
- **超时**：单次查询 30s，并通过 MySQL `MAX_EXECUTION_TIME` 服务端兜底。
- **审计**：每次 `run_query` 在 `audit.log_path` 落一行 JSON（who / what / rows / ok）。
- 强烈建议给每个 source 用**只读账号**（仅授 `SELECT`），再加一层 DB 侧防线。

## 配置

两份文件：

- `config.yaml`：拓扑 + 鉴权规则，**只引用** env 变量名，可安全 review。
- `.env.secrets`：实际密码 + token 值，**不可入库**。

参考 `config.example.yaml` / `.env.secrets.example`：

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

`allow` 规则三种形式：`source.database`、`source.*`、`*`。

生成 token：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 部署

工作流：**本地构建镜像 → `docker save` 打 tar 包 → scp 到服务器 → `docker load`
→ 起容器**。项目不上 Docker Hub，每次更新都重复这套流程。

容器直接绑 `0.0.0.0:8000`，部署后服务在 `http://<服务器IP>:8000/mcp` 上。
想要 HTTPS / 域名，再在前面套一层 nginx + TLS（见末尾「可选：nginx + HTTPS」）。

### 前置

- **本地（Windows + Docker Desktop）**：能跑 `docker build`，能 scp（PowerShell
  自带 `scp` 或用 WSL）。
- **服务器（Ubuntu 24.04）**：已装 Docker。
- **防火墙/安全组**：开放 **8000**（若套了 nginx 则开 **80/443**，8000 可只留内网）。

---

### Phase 1 · 本地构建并打包镜像

在仓库根目录（Windows，PowerShell）：

```powershell
# 1) 构建镜像（固定 tag latest，compose 里也写死 latest）
docker build -t mysql-readonly-mcp:latest .

# 2) 导出成 tar 包
docker save -o mysql-readonly-mcp.tar mysql-readonly-mcp:latest

# 3) 看一下文件大小（约 150-250 MB，正常）
Get-Item mysql-readonly-mcp.tar | Select-Object Name, Length
```

> **跨架构提醒**：本地和服务器都是 x86_64 时跳过此条。本地若是 Apple Silicon
> (M1/M2/...)，要强制 amd64：
> `docker build --platform linux/amd64 -t mysql-readonly-mcp:latest .`

---

### Phase 2 · 传文件到服务器

首次部署传 tar + compose + 配置模板；后续只更新镜像时只传 tar 包。

假设服务器上目标路径 `/srv/mcp`，远端用户 `ubuntu`：

```powershell
# 服务器先建好目录
ssh ubuntu@<服务器IP> "sudo mkdir -p /srv/mcp/logs && sudo chown -R ubuntu:ubuntu /srv/mcp"

# 一次性把镜像 + 部署所需文件 scp 上去
scp mysql-readonly-mcp.tar `
    docker-compose.yml `
    config.example.yaml `
    .env.secrets.example `
    ubuntu@<服务器IP>:/srv/mcp/
```

---

### Phase 3 · 服务器侧加载并启动

SSH 上去 `cd /srv/mcp`，然后：

```bash
# 1) 把镜像 tar 加载进本机 Docker
docker load -i mysql-readonly-mcp.tar
docker images | grep mysql-readonly-mcp        # 确认 mysql-readonly-mcp:latest 在

# 2) 准备配置文件
cp config.example.yaml config.yaml             # 编辑 source 拓扑 + token 规则
nano config.yaml

cp .env.secrets.example .env.secrets           # 填 DB 密码 + 生成每人的 token
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # 跑多次给每人取一个
nano .env.secrets
chmod 600 .env.secrets                         # 收紧权限

# 3) 起容器（不会 build，compose 里没 build: 段，直接用已加载的镜像）
docker compose up -d
docker compose ps                              # 应显示 mcp 是 running

# 4) 自检
curl -i http://127.0.0.1:8000/mcp              # 期望 401 + WWW-Authenticate
```

如果 4) 没返回 401，看日志：`docker compose logs mcp`。

---

### Phase 4 · 端到端验证

```bash
curl -i http://<服务器IP>:8000/mcp                                       # 401（没带 token）
curl -i -H "Authorization: Bearer <某个token>" http://<服务器IP>:8000/mcp # 200 / SSE 数据
```

本机用 MCP Inspector 跑全流程：

```powershell
npx @modelcontextprotocol/inspector
# URL: http://<服务器IP>:8000/mcp
# Header: Authorization: Bearer <你的 token>
# 依次：list_sources → list_tables → run_query "SELECT 1"
```

---

### 后续运维

**只更新代码（修了 bug 或加了 feature）**：
```powershell
# 本地：改完代码 → 重新打包（tag 永远 latest）
docker build -t mysql-readonly-mcp:latest .
docker save -o mysql-readonly-mcp.tar mysql-readonly-mcp:latest
scp mysql-readonly-mcp.tar ubuntu@<服务器IP>:/srv/mcp/
```
```bash
# 服务器
cd /srv/mcp
docker load -i mysql-readonly-mcp.tar
docker compose up -d              # 检测到镜像变了，自动重建容器
docker image prune -f             # 清理被覆盖的旧 latest（悬空镜像）
```

**只改配置（加/删/改用户 / 改库）**：
```bash
# 服务器
nano /srv/mcp/config.yaml          # 和/或 .env.secrets
docker compose -f /srv/mcp/docker-compose.yml restart mcp
```

**看审计 / 排查**：
```bash
tail -f /srv/mcp/logs/audit.log                          # 每次 run_query 一行 JSON
docker compose -f /srv/mcp/docker-compose.yml logs -f mcp # 运行日志
```

---

### 可选：nginx + HTTPS

要域名 + HTTPS 时，在宿主机用 nginx 反代到 `127.0.0.1:8000`（仓库自带
`nginx-mcp.conf.example`，已针对 streamable-http 调好缓冲/超时）：

```bash
# DNS：mcp.yourdomain.com 的 A 记录指向服务器公网 IP
sudo cp nginx-mcp.conf.example /etc/nginx/sites-available/mcp.conf
sudo sed -i 's/mcp.yourdomain.com/<你的真实域名>/g' /etc/nginx/sites-available/mcp.conf
sudo ln -s /etc/nginx/sites-available/mcp.conf /etc/nginx/sites-enabled/
sudo certbot --nginx -d <你的真实域名>        # 自动签证书并改写 ssl_* 行，之后自动续签
sudo nginx -t && sudo systemctl reload nginx
```

之后客户端 URL 用 `https://<你的真实域名>/mcp`。

> 套了 nginx 后，若不想让 8000 端口也对公网暴露，可把 `docker-compose.yml`
> 的 `ports` 改成 `"127.0.0.1:8000:8000"`，只留 nginx 这条入口。
>
> nginx 模板里的关键点：`proxy_buffering off` / `proxy_request_buffering off`
> （streamable-http 是流式响应，缓冲会卡住）、`proxy_read_timeout 600s`
> （SSE 连接挂得久）、`proxy_http_version 1.1` + `Connection ""`（保活复用）。

## 客户端配置（OpenClaw）

```json
{
  "mcp": {
    "servers": {
      "mysql-readonly": {
        "url": "http://<服务器IP>:8000/mcp",
        "transport": "streamable-http",
        "headers": {
          "Authorization": "Bearer <你的 token>"
        }
      }
    }
  }
}
```

套了 nginx + HTTPS 后，把 `url` 换成 `https://<你的真实域名>/mcp`，其余相同。

> `transport: "streamable-http"` 必须显式声明——不写时 OpenClaw 默认走 SSE。

## 项目结构

```
.
├── main.py                       # entrypoint：load config → build app → uvicorn.run
├── src/
│   ├── config.py                 # YAML + env 解析与校验
│   ├── validator.py              # 只读 SQL 校验
│   ├── db.py                     # pymysql 连接 & 执行
│   ├── auth.py                   # Bearer token ASGI middleware
│   ├── audit.py                  # JSON-line 审计日志
│   └── server.py                 # FastMCP 工具 + ASGI 组装
├── config.example.yaml
├── .env.secrets.example
├── Dockerfile
├── docker-compose.yml             # MCP 容器，绑 0.0.0.0:8000
└── nginx-mcp.conf.example         # 可选：宿主机 nginx server block 示例（HTTPS/域名）
```

## 本地开发（不用 Docker）

```bash
uv sync
export CONFIG_PATH=./config.yaml
set -a; source ./.env.secrets; set +a
uv run main.py
```
