"""Entrypoint: load config, build ASGI app, run uvicorn."""

from __future__ import annotations

import os

import uvicorn

from src.config import load_config
from src.server import build_app


def main() -> None:
    config_path = os.environ.get("CONFIG_PATH", "config.yaml")
    config = load_config(config_path)
    app = build_app(config)
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level="info",
        loop=config.server.loop,
    )


if __name__ == "__main__":
    main()
