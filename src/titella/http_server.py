"""MCP server shared over Streamable HTTP on top of FastAPI."""
from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from . import __version__
from .config import Config, load_config
from .mcp_server import create_server
from .registry import TitellaRegistry

LOOPBACK_HOST = "127.0.0.1"
SERVICE_NAME = "titella"
TRANSPORT_NAME = "streamable-http"


def create_app(config: Config) -> FastAPI:
    """Creates a single application that owns the Titella registry and all remote state."""
    # The same registry feeds the MCP server and the firmware health probe so
    # a single SSE/HTTP session never opens a second UART file descriptor.
    registry = TitellaRegistry(config)
    mcp_app = create_server(config, registry=registry).streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(
        title="Titella MCP",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        snapshot = await run_in_threadpool(registry.health_snapshot)
        return {
            "ok": True,
            "service": SERVICE_NAME,
            "transport": TRANSPORT_NAME,
            "pid": os.getpid(),
            "version": __version__,
            # The ESP32 firmware version cannot be inferred from the Python
            # package: it is queried from the firmware itself.
            **snapshot,
        }

    app.mount("/", mcp_app)
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Shared Streamable HTTP server of the ESP32-S3 HID auditor",
    )
    parser.add_argument("--config", help="path to the configuration TOML")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    uvicorn.run(
        create_app(config),
        host=LOOPBACK_HOST,
        port=config.http_port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
