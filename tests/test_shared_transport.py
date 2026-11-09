from __future__ import annotations

import asyncio
import http.client
import json
import os
import signal
import socket
import stat
import sys
import threading
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from titella import __version__
from titella.config import load_config
from titella.documentation import SERVER_INSTRUCTIONS
from titella.stdio_proxy import ensure_http_server
from titella_slave_assets import __version__ as assets_version

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "screen", "lock_acquire", "mouse_rel", "slave_bootstrap", "remote_shell",
    "remote_terminal_open", "remote_fs_read", "remote_upload", "remote_download",
    "kvm_status", "docs_index", "docs_read",
}
DOCUMENT_IDS = {
    "bootstrap-python", "bootstrap-windows", "getting-started", "mcp-transport",
    "operation", "serial-protocol", "troubleshooting",
}


def _text_payload(result) -> dict:
    for item in result.content:
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    raise AssertionError("the MCP response did not contain text")


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class SharedTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.port = _free_tcp_port()
        self.environment = dict(os.environ)
        self.environment.update({
            # Explicit path: the operator's TOML must not leak into the test.
            # No PYTHONPATH is injected: the wrapper subprocess must resolve
            # the installed packages of this environment.
            "TITELLA_CONFIG": str(self.root / "absent.toml"),
            "TITELLA_STATE_DIR": str(self.state),
            "TITELLA_HTTP_PORT": str(self.port),
        })
        self.addCleanup(self._stop_server)

    def _health(self) -> dict | None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=0.5)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            body = response.read()
        except OSError:
            return None
        finally:
            connection.close()
        if response.status != 200:
            return None
        value = json.loads(body)
        return value if isinstance(value, dict) else None

    def _wait_absent(self, pid: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            health = self._health()
            if not health or health.get("pid") != pid:
                return
            time.sleep(0.05)
        self.fail(f"the shared server {pid} did not stop")

    def _stop_server(self) -> None:
        health = self._health()
        if not health or health.get("service") != "titella":
            return
        pid = int(health["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        self._wait_absent(pid)

    async def _client(self, index: int) -> set[str]:
        error_path = self.root / f"wrapper-{index}.stderr"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "titella.stdio_proxy"],
            env=self.environment,
            cwd=str(ROOT),
        )
        with error_path.open("w") as error_stream:
            async with stdio_client(parameters, errlog=error_stream) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.serverInfo.name, "titella")
                    # initialize must announce the service version, not the MCP
                    # SDK's, and it must match /health.
                    self.assertEqual(initialized.serverInfo.version, __version__)
                    names = {tool.name for tool in (await session.list_tools()).tools}
        self.assertEqual(error_path.read_text(), "")
        return names

    def test_documentation_is_available_over_stdio_and_http_without_agent(self) -> None:
        """Docs read packaged assets only: no lease, no gate, no HID.

        The daemon in this test has no slave at all: the gate is closed and no
        lease is held. Both transports must still serve the catalog and the
        same Markdown bytes through tools and resources, and no operational
        error may appear.
        """

        async def stdio_scenario() -> None:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "titella.stdio_proxy"],
                env=self.environment,
                cwd=str(ROOT),
            )
            error_path = self.root / "docs-wrapper.stderr"
            with error_path.open("w") as error_stream:
                async with stdio_client(parameters, errlog=error_stream) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        self.assertEqual(
                            initialized.instructions, SERVER_INSTRUCTIONS
                        )
                        await self._assert_documentation_surface(session)
            self.assertEqual(error_path.read_text(), "")

        asyncio.run(stdio_scenario())

        async def http_scenario() -> None:
            async with streamable_http_client(
                f"http://127.0.0.1:{self.port}/mcp"
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.instructions, SERVER_INSTRUCTIONS)
                    await self._assert_documentation_surface(session)

        asyncio.run(http_scenario())

        # The gate is still closed: a remote operation fails fast with
        # agent_not_ready, proving the documentation calls above never needed it.
        async def gate_scenario() -> None:
            async with streamable_http_client(
                f"http://127.0.0.1:{self.port}/mcp"
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    blocked = _text_payload(
                        await session.call_tool("remote_shell", {"cmd": "printf x"})
                    )
                    self.assertEqual(blocked["status"], "error")
                    self.assertEqual(blocked["error"]["code"], "agent_not_ready")

        asyncio.run(gate_scenario())

    async def _assert_documentation_surface(self, session: ClientSession) -> None:
        index = _text_payload(await session.call_tool("docs_index", {}))
        ids = [entry["id"] for entry in index["documents"]]
        self.assertEqual(ids, sorted(DOCUMENT_IDS))
        windows = next(entry for entry in index["documents"]
                       if entry["id"] == "bootstrap-windows")
        self.assertEqual(windows["source"], "titella-slave-assets")
        self.assertEqual(windows["uri"], "titella://docs/bootstrap-windows")
        self.assertEqual(windows["version"], assets_version)

        loaded = _text_payload(
            await session.call_tool("docs_read", {"document_id": "bootstrap-windows"})
        )
        self.assertEqual(loaded["id"], "bootstrap-windows")
        self.assertEqual(loaded["mime_type"], "text/markdown")
        self.assertTrue(loaded["content"].strip())

        server_doc = _text_payload(
            await session.call_tool("docs_read", {"document_id": "getting-started"})
        )
        self.assertTrue(server_doc["content"].strip())
        self.assertEqual(server_doc["source"], "titella")

        for bad in ("", "../config.toml", "unknown-document", "/etc/passwd"):
            rejected = _text_payload(
                await session.call_tool("docs_read", {"document_id": bad})
            )
            self.assertEqual(rejected["status"], "error")
            self.assertEqual(rejected["error"]["code"], "doc_not_found")
            self.assertFalse(rejected["error"]["retryable"])

        resources = (await session.list_resources()).resources
        uris = {str(resource.uri) for resource in resources}
        self.assertEqual(uris, {f"titella://docs/{document_id}" for document_id in DOCUMENT_IDS})
        announced = next(resource for resource in resources
                         if str(resource.uri) == "titella://docs/bootstrap-windows")
        self.assertEqual(announced.mimeType, "text/markdown")
        self.assertTrue(announced.name)
        self.assertTrue(announced.description)

        read = await session.read_resource("titella://docs/bootstrap-windows")
        body = read.contents[0]
        self.assertEqual(body.mimeType, "text/markdown")
        self.assertEqual(body.text, loaded["content"])

    def test_concurrent_wrappers_share_one_server_and_restart_after_failure(self) -> None:
        async def concurrent_clients() -> list[set[str]]:
            return await asyncio.gather(*(self._client(index) for index in range(24)))

        tool_sets = asyncio.run(concurrent_clients())
        for names in tool_sets:
            self.assertLessEqual(EXPECTED_TOOLS, names)

        health = self._health()
        self.assertIsNotNone(health)
        assert health is not None
        first_pid = int(health["pid"])
        self.assertEqual(health["transport"], "streamable-http")
        self.assertEqual(health["version"], __version__)

        # The wrappers have exited, but the UART owner is still alive.
        self.assertEqual(self._health()["pid"], first_pid)  # type: ignore[index]
        self.assertEqual(stat.S_IMODE((self.state / f"http-{self.port}.lock").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.state / f"http-{self.port}.log").stat().st_mode), 0o600)

        os.kill(first_pid, signal.SIGTERM)
        self._wait_absent(first_pid)
        restarted_tools = asyncio.run(self._client(99))
        self.assertLessEqual(EXPECTED_TOOLS, restarted_tools)
        restarted = self._health()
        self.assertIsNotNone(restarted)
        assert restarted is not None
        self.assertNotEqual(int(restarted["pid"]), first_pid)

    def test_foreign_health_endpoint_is_not_replaced(self) -> None:
        self._assert_health_rejected(b'{"service":"other"}', "busy")

    def test_stale_version_is_reported_with_actionable_pid(self) -> None:
        self._assert_health_rejected(
            b'{"service":"titella","transport":"streamable-http","version":"0.9.0","pid":4242}',
            r"version 0\.9\.0.*4242",
        )

    def _assert_health_rejected(self, payload: bytes, pattern: str) -> None:
        class ForeignHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *args: object) -> None:
                return

        foreign = ThreadingHTTPServer(("127.0.0.1", self.port), ForeignHandler)
        thread = threading.Thread(target=foreign.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(foreign.server_close)
        self.addCleanup(foreign.shutdown)
        config = load_config(environ={
            "TITELLA_STATE_DIR": str(self.state),
            "TITELLA_HTTP_PORT": str(self.port),
        })
        with self.assertRaisesRegex(RuntimeError, pattern):
            ensure_http_server(config)


if __name__ == "__main__":
    unittest.main()
