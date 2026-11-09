"""MCP stdio proxy towards the server shared over Streamable HTTP."""
from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import anyio
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server

from . import __version__
from .config import Config, load_config
from .http_server import LOOPBACK_HOST, SERVICE_NAME, TRANSPORT_NAME

START_TIMEOUT_S = 15.0
PROBE_TIMEOUT_S = 0.5
_STARTED_PROCESSES: list[subprocess.Popen[bytes]] = []


def _health_path() -> str:
    return "/health"


def _mcp_url(config: Config) -> str:
    return f"http://{LOOPBACK_HOST}:{config.http_port}/mcp"


def _probe(config: Config) -> tuple[str, dict[str, Any] | str | None]:
    connection = http.client.HTTPConnection(
        LOOPBACK_HOST,
        config.http_port,
        timeout=PROBE_TIMEOUT_S,
    )
    try:
        connection.request("GET", _health_path(), headers={"Accept": "application/json"})
        response = connection.getresponse()
        body = response.read(64 * 1024)
    except (OSError, http.client.HTTPException):
        return "absent", None
    finally:
        connection.close()

    if response.status != 200:
        return "foreign", f"HTTP {response.status} at {_health_path()}"
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "foreign", "health response is not JSON"
    if not isinstance(payload, dict):
        return "foreign", "health response is not an object"
    if payload.get("service") != SERVICE_NAME or payload.get("transport") != TRANSPORT_NAME:
        return "foreign", "the port belongs to another service"
    stale = payload.get("version")
    if stale != __version__:
        return "stale", (
            f"the daemon at {LOOPBACK_HOST}:{config.http_port} runs version "
            f"{stale} and this wrapper expects {__version__}; stop pid "
            f"{payload.get('pid')} so the new version starts"
        )
    return "ready", payload


def _log_tail(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-4000:].strip()
    except OSError:
        return ""


def ensure_http_server(config: Config, config_path: str | None = None) -> dict[str, Any]:
    """Returns the daemon health or starts it exactly once across processes."""
    state_dir = config.ensure_state_dir()
    lock_path = state_dir / f"http-{config.http_port}.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        with os.fdopen(lock_fd, "a+") as lock_stream:
            lock_fd = -1
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
            state, detail = _probe(config)
            if state == "ready":
                return dict(detail or {})
            if state == "foreign":
                raise RuntimeError(f"port {config.http_port} is busy: {detail}")
            if state == "stale":
                # We do not replace it: it may hold live MCP sessions and remote
                # state. Failing with the pid lets the operator restart it.
                raise RuntimeError(str(detail))

            log_path = state_dir / f"http-{config.http_port}.log"
            log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            os.fchmod(log_fd, 0o600)
            command = [sys.executable, "-m", "titella.http_server"]
            if config_path:
                command.extend(["--config", config_path])
            with os.fdopen(log_fd, "wb", buffering=0) as log_stream:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                    env=dict(os.environ),
                )
            _STARTED_PROCESSES.append(process)

            deadline = time.monotonic() + START_TIMEOUT_S
            while time.monotonic() < deadline:
                state, detail = _probe(config)
                if state == "ready":
                    return dict(detail or {})
                if process.poll() is not None:
                    output = _log_tail(log_path)
                    suffix = f": {output}" if output else ""
                    raise RuntimeError(
                        f"the HTTP server exited with code {process.returncode}{suffix}",
                    )
                time.sleep(0.05)
            process.terminate()
            try:
                process.wait(2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(2)
            raise RuntimeError(
                f"the HTTP server did not respond within {START_TIMEOUT_S:g} seconds; "
                f"log: {log_path}",
            )
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)


async def _forward(receive: Any, send: Any, cancel_scope: anyio.CancelScope) -> None:
    try:
        async with receive:
            async for item in receive:
                if isinstance(item, Exception):
                    raise item
                await send.send(item)
    finally:
        cancel_scope.cancel()


async def _bridge(url: str) -> None:
    async with stdio_server() as (stdio_read, stdio_write):
        try:
            async with streamable_http_client(url) as (http_read, http_write, _session_id):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(_forward, stdio_read, http_write, tasks.cancel_scope)
                    tasks.start_soon(_forward, http_read, stdio_write, tasks.cancel_scope)
        finally:
            await stdio_write.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="MCP stdio proxy to the shared server of the ESP32-S3 HID auditor",
    )
    parser.add_argument("--config", help="path to the configuration TOML")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        ensure_http_server(config, args.config)
        anyio.run(_bridge, _mcp_url(config))
    except KeyboardInterrupt:
        return
    except Exception as exc:
        print(f"titella-mcp: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
