"""Load and validate the YAML config, resolving secrets from environment variables.

Sensitive values (DB passwords, bearer tokens) live in env vars referenced from
the YAML by name (`password_env`, `token_env`). The YAML itself is safe to
review and version-control.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_IDENT = re.compile(r"[A-Za-z0-9_-]+")


def _ident(value: str, kind: str) -> str:
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise ValueError(f"Invalid {kind}: {value!r} (allowed: letters, digits, _ -)")
    return value


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Required environment variable {name} is missing or empty.")
    return value


@dataclass(frozen=True)
class SourceConfig:
    name: str
    host: str
    port: int
    user: str
    password: str
    databases: tuple[str, ...]


@dataclass(frozen=True)
class TokenConfig:
    label: str
    token: str
    allow: tuple[str, ...]  # entries like "mysql-1.orders", "mysql-2.*", or "*"

    def can_access(self, source: str, database: str) -> bool:
        target = f"{source}.{database}"
        for rule in self.allow:
            if rule == "*" or rule == target:
                return True
            if rule.endswith(".*") and source == rule[:-2]:
                return True
        return False

    def visible_pairs(self, sources: dict[str, SourceConfig]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for src_name, src in sources.items():
            for db in src.databases:
                if self.can_access(src_name, db):
                    out.append((src_name, db))
        return out


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    name: str = "mysql-readonly"
    # Optional deployment-specific text appended after the built-in tool
    # instructions; describe business context, not tool behavior.
    instructions: str | None = None
    default_row_limit: int = 100
    max_row_limit: int = 1000


@dataclass(frozen=True)
class AuditConfig:
    log_path: str | None = None


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    sources: dict[str, SourceConfig]
    tokens: tuple[TokenConfig, ...]
    audit: AuditConfig
    _by_token: dict[str, TokenConfig] = field(default_factory=dict)

    def token_for(self, raw_token: str) -> TokenConfig | None:
        return self._by_token.get(raw_token)

    def source(self, name: str) -> SourceConfig:
        try:
            return self.sources[name]
        except KeyError as exc:
            raise ValueError(f"Unknown source: {name!r}") from exc


def _parse_source(name: str, raw: dict[str, Any]) -> SourceConfig:
    name = _ident(name, "source name")
    host = raw.get("host")
    if not host:
        raise ValueError(f"Source {name!r}: 'host' is required.")
    user = raw.get("user")
    if not user:
        raise ValueError(f"Source {name!r}: 'user' is required.")
    password_env = raw.get("password_env")
    if not password_env:
        raise ValueError(f"Source {name!r}: 'password_env' is required.")
    databases = raw.get("databases") or []
    if not databases:
        raise ValueError(f"Source {name!r}: 'databases' must list at least one DB.")
    dbs = tuple(_ident(d, "database name") for d in databases)
    return SourceConfig(
        name=name,
        host=str(host),
        port=int(raw.get("port", 3306)),
        user=str(user),
        password=_require_env(str(password_env)),
        databases=dbs,
    )


def _parse_token(raw: dict[str, Any], sources: dict[str, SourceConfig]) -> TokenConfig:
    label = raw.get("label")
    if not label:
        raise ValueError("Token entry missing 'label'.")
    label = _ident(str(label), "token label")
    token_env = raw.get("token_env")
    if not token_env:
        raise ValueError(f"Token {label!r}: 'token_env' is required.")
    allow = raw.get("allow") or []
    if not allow:
        raise ValueError(f"Token {label!r}: 'allow' must list at least one rule.")
    rules = tuple(_validate_rule(label, r, sources) for r in allow)
    return TokenConfig(label=label, token=_require_env(str(token_env)), allow=rules)


def _validate_rule(label: str, rule: Any, sources: dict[str, SourceConfig]) -> str:
    if not isinstance(rule, str) or not rule:
        raise ValueError(f"Token {label!r}: invalid allow rule {rule!r}.")
    if rule == "*":
        return rule
    if rule.endswith(".*"):
        src = rule[:-2]
        if src not in sources:
            raise ValueError(f"Token {label!r}: rule {rule!r} refers to unknown source.")
        return rule
    if "." not in rule:
        raise ValueError(
            f"Token {label!r}: rule {rule!r} must be '*', '<source>.*', or '<source>.<database>'."
        )
    src, db = rule.split(".", 1)
    if src not in sources:
        raise ValueError(f"Token {label!r}: rule {rule!r} refers to unknown source.")
    if db not in sources[src].databases:
        raise ValueError(
            f"Token {label!r}: rule {rule!r} refers to database not listed in source."
        )
    return rule


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    server_raw = raw.get("server") or {}
    name = str(server_raw.get("name") or "mysql-readonly").strip()
    if not name:
        raise ValueError("server.name must not be empty.")
    instructions = server_raw.get("instructions")
    instructions = str(instructions).strip() if instructions else None
    server = ServerConfig(
        host=str(server_raw.get("host", "0.0.0.0")),
        port=int(server_raw.get("port", 8000)),
        name=name,
        instructions=instructions or None,
        default_row_limit=int(server_raw.get("default_row_limit", 100)),
        max_row_limit=int(server_raw.get("max_row_limit", 1000)),
    )

    sources_raw = raw.get("sources") or {}
    if not sources_raw:
        raise ValueError("Config must define at least one source under 'sources'.")
    sources = {name: _parse_source(name, body or {}) for name, body in sources_raw.items()}

    tokens_raw = raw.get("tokens") or []
    if not tokens_raw:
        raise ValueError("Config must define at least one token under 'tokens'.")
    tokens = tuple(_parse_token(t or {}, sources) for t in tokens_raw)

    by_token: dict[str, TokenConfig] = {}
    for t in tokens:
        if t.token in by_token:
            raise ValueError(
                f"Duplicate token value: labels {by_token[t.token].label!r} and {t.label!r} share a token."
            )
        by_token[t.token] = t

    audit_raw = raw.get("audit") or {}
    audit = AuditConfig(log_path=audit_raw.get("log_path"))

    return AppConfig(
        server=server, sources=sources, tokens=tokens, audit=audit, _by_token=by_token
    )
