"""JSON-lines audit logger for run_query calls."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, log_path: str | None) -> None:
        self._path = Path(log_path) if log_path else None
        self._lock = threading.Lock()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        identity: str,
        source: str,
        database: str,
        sql: str,
        rows: int | None = None,
        truncated: bool | None = None,
        ok: bool,
        error: str | None = None,
    ) -> None:
        if self._path is None:
            return
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "identity": identity,
            "source": source,
            "database": database,
            "sql": sql,
            "ok": ok,
        }
        if rows is not None:
            entry["rows"] = rows
        if truncated is not None:
            entry["truncated"] = truncated
        if error is not None:
            entry["error"] = error
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
