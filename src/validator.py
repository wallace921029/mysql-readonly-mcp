"""Application-layer read-only enforcement for SQL submitted via run_query.

Combined with a least-privileged DB user, this is the second line of defense
that ensures the MCP can only read."""

from __future__ import annotations

import re

_ALLOWED_LEADING_KEYWORDS = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH"}

_FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE",
    "DROP", "CREATE", "ALTER", "TRUNCATE", "RENAME",
    "GRANT", "REVOKE", "SET", "LOCK", "UNLOCK",
    "CALL", "LOAD", "HANDLER", "INTO",
}


def _strip_sql(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"(--|#)[^\n]*", " ", sql)
    return sql.strip().rstrip(";").strip()


def validate_read_only(sql: str) -> str:
    """Return a cleaned, single read-only statement or raise ValueError."""
    cleaned = _strip_sql(sql)
    if not cleaned:
        raise ValueError("Empty query.")

    if ";" in cleaned:
        raise ValueError("Multiple statements are not allowed; submit one query at a time.")

    leading = re.match(r"\s*([A-Za-z]+)", cleaned)
    if not leading or leading.group(1).upper() not in _ALLOWED_LEADING_KEYWORDS:
        raise ValueError(
            "Only read-only queries are allowed "
            "(SELECT, SHOW, DESCRIBE, EXPLAIN, WITH)."
        )

    tokens = {t.upper() for t in re.findall(r"[A-Za-z_]+", cleaned)}
    forbidden = tokens & _FORBIDDEN_KEYWORDS
    if forbidden:
        raise ValueError(
            f"Query contains forbidden keyword(s): {', '.join(sorted(forbidden))}."
        )
    return cleaned
