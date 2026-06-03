"""Per-source MySQL connection helper.

A fresh connection is opened for each call. At our scale (occasional human-
driven LLM queries) this is fine; introduce a pool later if traffic grows."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pymysql

from .config import SourceConfig

QUERY_TIMEOUT_SECONDS = 30


@contextmanager
def _connect(source: SourceConfig, database: str) -> Iterator[pymysql.connections.Connection]:
    conn = pymysql.connect(
        host=source.host,
        port=source.port,
        user=source.user,
        password=source.password,
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        read_timeout=QUERY_TIMEOUT_SECONDS,
        write_timeout=QUERY_TIMEOUT_SECONDS,
        connect_timeout=QUERY_TIMEOUT_SECONDS,
    )
    try:
        yield conn
    finally:
        conn.close()


def execute(
    source: SourceConfig,
    database: str,
    sql: str,
    params: tuple | None = None,
) -> list[dict[str, Any]]:
    """Run a single statement against (source, database) and return rows."""
    with _connect(source, database) as conn, conn.cursor() as cur:
        # Server-side execution-time cap (ms) as a second guard.
        cur.execute(f"SET SESSION MAX_EXECUTION_TIME={QUERY_TIMEOUT_SECONDS * 1000}")
        cur.execute(sql, params)
        rows = cur.fetchall()
        return list(rows) if rows is not None else []
