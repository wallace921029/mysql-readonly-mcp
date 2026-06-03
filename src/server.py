"""FastMCP tool definitions and ASGI app assembly."""

from __future__ import annotations

import re
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.types import ASGIApp

from . import db
from .audit import AuditLogger
from .auth import BearerAuthMiddleware, require_identity
from .config import AppConfig
from .validator import validate_read_only

DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 1000

# Core, behavior-describing instructions. These track the actual tool
# implementation (read-only gate, row cap) and must stay in sync with the code,
# so they live here rather than in config. A deployment may append extra
# business context via server.instructions in config.yaml.
CORE_INSTRUCTIONS = (
    "Read-only access to one or more MySQL databases. "
    "Call list_sources first to see which (source, database) pairs your "
    "token can access; pass those names to the other tools. "
    "list_tables and describe_table inspect schema; run_query executes a "
    "single read-only statement (SELECT/SHOW/DESCRIBE/EXPLAIN/WITH only — "
    "writes, DDL, and stacked queries are rejected). Results are capped at "
    "1000 rows. Never assume a source/database exists without listing it first."
)

_IDENT = re.compile(r"[A-Za-z0-9_-]+")
_TABLE_IDENT = re.compile(r"[A-Za-z0-9_]+")

# Reusable parameter annotations — descriptions flow into the tools' JSON schema.
SourceArg = Annotated[
    str, Field(description="MySQL source name, as returned by list_sources.")
]
DatabaseArg = Annotated[
    str,
    Field(description="Database within that source, as returned by list_sources."),
]


def _resolve(config: AppConfig, source: str, database: str):
    if not _IDENT.fullmatch(source):
        raise ValueError(f"Invalid source name: {source!r}")
    if not _IDENT.fullmatch(database):
        raise ValueError(f"Invalid database name: {database!r}")
    identity = require_identity()
    if not identity.can_access(source, database):
        raise PermissionError(
            f"Token {identity.label!r} is not permitted to access {source}.{database}."
        )
    src = config.source(source)
    if database not in src.databases:
        raise ValueError(f"Database {database!r} is not exposed under source {source!r}.")
    return identity, src


def build_app(config: AppConfig) -> ASGIApp:
    instructions = CORE_INSTRUCTIONS
    if config.server.instructions:
        instructions = f"{instructions}\n\n{config.server.instructions}"
    mcp = FastMCP(
        config.server.name,
        # Disable the SDK's DNS-rebinding Host-header check. It otherwise
        # auto-enables (FastMCP's default host is 127.0.0.1) and rejects any
        # request whose Host isn't localhost — i.e. every request that arrives
        # via our domain or server IP ("Invalid Host header"). That protection
        # guards against ambient-credential attacks (cookies); we authenticate
        # with an explicit Bearer token, so it adds nothing here.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
        instructions=instructions,
    )
    audit = AuditLogger(config.audit.log_path)

    @mcp.tool()
    def list_sources() -> list[dict[str, str]]:
        """List the (source, database) pairs the current token can access."""
        identity = require_identity()
        return [
            {"source": s, "database": d}
            for s, d in identity.visible_pairs(config.sources)
        ]

    @mcp.tool()
    def list_tables(source: SourceArg, database: DatabaseArg) -> list[str]:
        """List all table names in the given (source, database)."""
        _, src = _resolve(config, source, database)
        rows = db.execute(src, database, "SHOW TABLES")
        return [next(iter(row.values())) for row in rows]

    @mcp.tool()
    def describe_table(
        source: SourceArg,
        database: DatabaseArg,
        table_name: Annotated[
            str, Field(description="Table to describe; must already exist in the database.")
        ],
    ) -> list[dict[str, Any]]:
        """Return column structure (name, type, nullability, key, default) of a table."""
        _, src = _resolve(config, source, database)
        if not _TABLE_IDENT.fullmatch(table_name):
            raise ValueError("Invalid table name.")
        return db.execute(src, database, f"DESCRIBE `{table_name}`")

    @mcp.tool()
    def run_query(
        source: SourceArg,
        database: DatabaseArg,
        sql: Annotated[
            str,
            Field(
                description=(
                    "A single read-only statement starting with "
                    "SELECT/SHOW/DESCRIBE/EXPLAIN/WITH. Writes, DDL, and stacked "
                    "queries (multiple statements separated by ';') are rejected."
                )
            ),
        ],
        row_limit: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_ROW_LIMIT,
                description="Maximum rows returned (1-1000); excess is truncated.",
            ),
        ] = DEFAULT_ROW_LIMIT,
    ) -> dict[str, Any]:
        """Execute a read-only SQL query and return rows."""
        identity, src = _resolve(config, source, database)
        row_limit = max(1, min(row_limit, MAX_ROW_LIMIT))
        try:
            cleaned = validate_read_only(sql)
            rows = db.execute(src, database, cleaned)
        except Exception as exc:
            audit.record(
                identity=identity.label,
                source=source,
                database=database,
                sql=sql,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        truncated = len(rows) > row_limit
        result = {
            "row_count": min(len(rows), row_limit),
            "truncated": truncated,
            "rows": rows[:row_limit],
        }
        audit.record(
            identity=identity.label,
            source=source,
            database=database,
            sql=cleaned,
            rows=result["row_count"],
            truncated=truncated,
            ok=True,
        )
        return result

    # streamable_http_app() returns a Starlette app already mounted at /mcp.
    # Wrap it as an ASGI middleware chain so we don't have to re-implement
    # FastMCP's internal route/lifespan plumbing.
    return BearerAuthMiddleware(mcp.streamable_http_app(), config=config)
