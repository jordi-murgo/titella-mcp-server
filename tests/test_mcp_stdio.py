from __future__ import annotations

import asyncio
import errno
import base64
import http.client
import json
import os
import pty
import shutil
import subprocess
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from titella import __version__ as server_version
from titella_slave_assets import __version__ as assets_version
from titella_slave_assets.bundles import load_bundle
from titella_slave_assets.wire import (
    FIRMWARE_TO_MASTER,
    FIRMWARE_TO_SLAVE,
    MASTER_TO_FIRMWARE,
    SLAVE_TO_FIRMWARE,
    FrameParser,
    base_frame,
    encode_frame,
)

LINK = "0123456789abcdef0123456789abcdef"
ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION_IDS = (
    "bootstrap-python", "bootstrap-windows", "getting-started", "mcp-transport",
    "operation", "serial-protocol", "troubleshooting",
)


class FakeFirmware:
    """UART0↔CDC router in Python: runs HID and forwards the rest to the agent."""

    def __init__(self, master_fd: int, slave_fd: int) -> None:
        self.master_fd = master_fd
        self.slave_fd = slave_fd
        self.bind_id = 900000
        self.from_master = FrameParser(MASTER_TO_FIRMWARE, ignore_invalid=True)
        self.from_slave = FrameParser(SLAVE_TO_FIRMWARE, ignore_invalid=True)
        self.stop = threading.Event()
        self.hid_ops: list[str] = []
        self.threads = [
            threading.Thread(target=self._pump, args=(master_fd, self.from_master, True), daemon=True),
            threading.Thread(target=self._pump, args=(slave_fd, self.from_slave, False), daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def _write(self, fd: int, frame: dict, direction: str) -> None:
        payload = encode_frame(frame, direction)
        try:
            os.write(fd, payload)
        except OSError:
            pass

    def _pump(self, fd: int, parser: FrameParser, from_master: bool) -> None:
        while not self.stop.is_set():
            try:
                import select
                readable, _, _ = select.select([fd], [], [], 0.1)
                if not readable:
                    continue
                chunk = os.read(fd, 4096)
            except OSError as exc:
                # Linux PTY masters report EIO while no slave fd is open.
                # A daemon restart intentionally creates that interval.
                if exc.errno == errno.EIO:
                    time.sleep(0.01)
                    continue
                return
            if not chunk:
                # macOS/BSD PTY masters return b"" (EOF) instead of EIO while
                # no slave descriptor is open across a daemon restart.
                time.sleep(0.01)
                continue
            for frame in parser.feed(chunk):
                if from_master and frame["channel"] == "hid":
                    self.hid_ops.append(frame.get("op", ""))
                    self._write(self.master_fd, base_frame(
                        LINK, "hid", "response",
                        request_id=frame["request_id"], op=frame["op"],
                        body={"ok": True, "status": "hid_ready"},
                    ), FIRMWARE_TO_MASTER)
                elif from_master:
                    self._write(self.slave_fd, frame, FIRMWARE_TO_SLAVE)
                else:
                    if frame["type"] == "hello":
                        # The real master sends control.bind after agent.hello.
                        self._write(self.slave_fd, base_frame(
                            LINK, "control", "request",
                            request_id=self.bind_id, op="bind",
                            body={"link_id": LINK},
                        ), FIRMWARE_TO_SLAVE)
                        self.bind_id += 1
                    self._write(self.master_fd, frame, FIRMWARE_TO_MASTER)

    def close(self) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(2)


def _payload_text(result) -> dict:
    import json
    for item in result.content:
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    raise AssertionError("the MCP response did not contain text")


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class McpStdioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.http_port = _free_tcp_port()
        state = self.root / "state"
        state.mkdir(mode=0o700)
        (state / "active-link-id").write_text(LINK, encoding="ascii")
        # Wheel-only contract: the payload comes from the installed
        # titella-slave-assets wheel, never from a repository build.
        self.agent_payload = load_bundle("python").payload
        self.agent = self.root / "agent.pyz"
        self.agent.write_bytes(self.agent_payload)
        self.agent.chmod(0o700)

        self.uart_master, uart_slave = pty.openpty()
        self.uart_name = os.ttyname(uart_slave)
        self.cdc_master, cdc_slave = pty.openpty()
        cdc_name = os.ttyname(cdc_slave)
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        runtime = shutil.which("python3.9") or sys.executable
        self.agent_process = subprocess.Popen(
            [runtime, str(self.agent), "--port", cdc_name, "--link-id", LINK],
            env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Keep no extra slave descriptors in the harness. In particular, a
        # leaked UART slave keeps Linux TIOCEXCL active after the first daemon
        # exits and makes a legitimate reconnect look permanently busy.
        os.close(uart_slave)
        os.close(cdc_slave)
        self.addCleanup(self._stop_agent)
        self.firmware = FakeFirmware(self.uart_master, self.cdc_master)
        self.addCleanup(self.firmware.close)

        self.server_env = dict(os.environ)
        self.server_env.update({
            # Explicit path: the operator's TOML must not leak into the test.
            # No PYTHONPATH is injected: every subprocess must resolve the
            # installed titella and titella_slave_assets packages of this
            # environment, so the suite also runs outside a checkout.
            "TITELLA_CONFIG": str(self.root / "absent.toml"),
            "TITELLA_SERIAL_PORT": self.uart_name,
            "TITELLA_STATE_DIR": str(state),
            "TITELLA_HEARTBEAT_S": "1",
            "TITELLA_DISCONNECT_GRACE_S": "5",
            "TITELLA_SCREEN_DEVICE": "no-such-capture-device",
            "TITELLA_HTTP_PORT": str(self.http_port),
        })
        self.addCleanup(lambda: os.close(self.uart_master))
        self.addCleanup(lambda: os.close(self.cdc_master))
        self.addCleanup(self._stop_http_server)

    def _stop_agent(self) -> None:
        if self.agent_process.poll() is None:
            self.agent_process.terminate()
        try:
            self.agent_process.wait(5)
        except subprocess.TimeoutExpired:
            self.agent_process.kill()
            self.agent_process.wait(5)

    def _http_health(self) -> dict | None:
        connection = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=0.5)
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

    def _stop_http_server(self) -> None:
        health = self._http_health()
        if not health or health.get("service") != "titella":
            return
        pid = int(health["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self._http_health()
            if not current or current.get("pid") != pid:
                return
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _run(self, scenario) -> None:
        async def main() -> None:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "titella.stdio_proxy"],
                env=self.server_env,
                cwd=str(ROOT),
            )
            errlog = self.root / "server-stderr.log"
            with errlog.open("w") as error_stream:
                async with stdio_client(parameters, errlog=error_stream) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await scenario(session)
            self.server_stderr = errlog.read_text()

        asyncio.run(main())

    def test_full_surface_over_stdio(self) -> None:
        async def scenario(session: ClientSession) -> None:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            expected = {
                "screen", "lock_acquire", "lock_renew", "lock_release", "lock_status",
                "mouse_abs", "mouse_rel", "click", "wheel", "key", "type", "keyboard_layout",
                "slave_bootstrap", "slave_status", "slave_stop",
                "remote_shell", "remote_shell_result", "remote_shell_cancel",
                "remote_terminal_open", "remote_terminal_read", "remote_terminal_write",
                "remote_terminal_resize", "remote_terminal_close",
                "remote_fs_read", "remote_fs_write", "remote_fs_list", "remote_fs_delete",
                "remote_fs_replace", "remote_fs_search", "remote_fs_find", "remote_sysinfo",
                "remote_upload", "remote_download", "remote_transfer_status", "remote_transfer_cancel",
                "kvm_status", "docs_index", "docs_read",
            }
            self.assertEqual(expected - tools, set())

            # Documentation comes from the installed wheels alone: this smoke
            # runs without a checkout, so a byte that appears here was packaged.
            hid_before_docs = list(self.firmware.hid_ops)
            index = _payload_text(await session.call_tool("docs_index", {}))
            catalog = {entry["id"]: entry for entry in index["documents"]}
            self.assertEqual(sorted(catalog), sorted(DOCUMENTATION_IDS))
            self.assertEqual(index["server_version"], server_version)
            self.assertEqual(index["slave_assets_version"], assets_version)
            self.assertEqual(catalog["getting-started"]["source"], "titella")
            self.assertEqual(catalog["getting-started"]["version"], server_version)
            self.assertEqual(catalog["bootstrap-python"]["source"], "titella-slave-assets")
            self.assertEqual(catalog["bootstrap-python"]["version"], assets_version)
            self.assertEqual(
                catalog["bootstrap-python"]["uri"], "titella://docs/bootstrap-python"
            )

            server_document = _payload_text(await session.call_tool(
                "docs_read", {"document_id": "getting-started"},
            ))
            self.assertEqual(server_document["mime_type"], "text/markdown")
            self.assertTrue(server_document["content"].strip())
            slave_document = _payload_text(await session.call_tool(
                "docs_read", {"document_id": "bootstrap-python"},
            ))
            self.assertTrue(slave_document["content"].strip())

            for bad in ("", "../config.toml", "unknown-document", "/etc/passwd"):
                rejected = _payload_text(await session.call_tool(
                    "docs_read", {"document_id": bad},
                ))
                self.assertEqual(rejected["status"], "error")
                self.assertEqual(rejected["error"]["code"], "doc_not_found")
                self.assertFalse(rejected["error"]["retryable"])
                self.assertNotIn("content", rejected)

            announced = {
                str(resource.uri) for resource in
                (await session.list_resources()).resources
            }
            self.assertEqual(
                announced, {f"titella://docs/{document_id}" for document_id in DOCUMENTATION_IDS}
            )
            for document_id, expected_document in (
                ("getting-started", server_document),
                ("bootstrap-python", slave_document),
            ):
                read = await session.read_resource(f"titella://docs/{document_id}")
                body = read.contents[0]
                self.assertEqual(body.mimeType, "text/markdown")
                self.assertEqual(body.text, expected_document["content"])
            self.assertEqual(self.firmware.hid_ops, hid_before_docs)

            self.assertTrue(_payload_text(await session.call_tool("lock_acquire", {"owner": "ops"}))["ok"])
            denied = _payload_text(await session.call_tool("lock_acquire", {"owner": "other"}))
            self.assertEqual(denied["status"], "error")
            self.assertEqual(denied["error"]["code"], "hid_lease_busy")
            self.assertTrue(denied["error"]["retryable"])
            invalid_relative = _payload_text(await session.call_tool(
                "mouse_rel", {"owner": "ops", "x": 200, "y": 0},
            ))
            self.assertEqual(invalid_relative["status"], "error")
            self.assertEqual(invalid_relative["error"]["code"], "invalid_argument")
            self.assertFalse(invalid_relative["error"]["retryable"])
            relative = _payload_text(await session.call_tool("mouse_rel", {"owner": "ops", "x": 3, "y": -2}))
            self.assertTrue(relative["ok"])
            self.assertIn("mouse_rel", self.firmware.hid_ops)

            # The slave platform is an explicit choice: without one the tool
            # fails before typing anything at the target, even through MCP.
            hid_before = list(self.firmware.hid_ops)
            missing_platform = _payload_text(await session.call_tool(
                "slave_bootstrap", {"owner": "ops"},
            ))
            self.assertEqual(missing_platform["status"], "error")
            self.assertEqual(missing_platform["error"]["code"], "slave_platform_required")
            self.assertFalse(missing_platform["error"]["retryable"])
            self.assertEqual(self.firmware.hid_ops, hid_before)

            # The layout precondition holds over the real stdio protocol too:
            # none of these rejections may reach the HID layer.
            missing_layout = _payload_text(await session.call_tool(
                "slave_bootstrap", {"owner": "ops", "platform": "macos"},
            ))
            self.assertEqual(missing_layout["status"], "error")
            self.assertEqual(missing_layout["error"]["code"], "slave_layout_required")
            unconfirmed = _payload_text(await session.call_tool(
                "slave_bootstrap", {
                    "owner": "ops", "platform": "macos", "layout": "es_ES",
                },
            ))
            self.assertEqual(unconfirmed["status"], "error")
            self.assertEqual(unconfirmed["error"]["code"], "slave_layout_unconfirmed")
            unsupported = _payload_text(await session.call_tool(
                "slave_bootstrap", {
                    "owner": "ops", "platform": "macos", "layout": "fr_FR",
                    "layout_confirmed": True,
                },
            ))
            self.assertEqual(unsupported["status"], "error")
            self.assertEqual(unsupported["error"]["code"], "invalid_argument")
            # A truthy non-boolean must not be coerced into an attestation.
            non_boolean = await session.call_tool("slave_bootstrap", {
                "owner": "ops", "platform": "macos", "layout": "en_US",
                "layout_confirmed": "true",
            })
            self.assertTrue(non_boolean.isError)
            self.assertEqual(self.firmware.hid_ops, hid_before)

            ready = _payload_text(await session.call_tool("slave_status", {}))
            self.assertEqual(ready["status"], "not_started")

            # screen reads from the local capture card: with a nonexistent device it fails cleanly.
            captured = await session.call_tool("screen", {})
            capture_body = _payload_text(captured)
            self.assertFalse(capture_body["ok"])
            self.assertEqual(capture_body["status"], "error")
            self.assertEqual(capture_body["error"]["code"], "screen_capture_failed")
            self.assertTrue(capture_body["error"]["retryable"])
            self.assertIn("no-such-capture-device", capture_body["error"]["message"])

            quick = _payload_text(await session.call_tool("remote_shell", {"cmd": "printf ok"}))
            self.assertEqual(quick["status"], "finished")
            self.assertEqual(quick["stdout"], "ok")
            self.assertIsNone(quick["job_id"])

            slow = _payload_text(await session.call_tool(
                "remote_shell", {"cmd": "sleep 5; printf late", "sync_timeout": 0.5},
            ))
            self.assertEqual(slow["status"], "running")
            self.assertIsInstance(slow["job_id"], str)
            pending = _payload_text(await session.call_tool(
                "remote_shell_result", {"job_id": slow["job_id"]},
            ))
            self.assertEqual(pending["status"], "running")
            cancelled = _payload_text(await session.call_tool(
                "remote_shell_cancel", {"job_id": slow["job_id"]},
            ))
            self.assertIn(cancelled["status"], {"cancelled", "finished", "error"})
            missing = _payload_text(await session.call_tool(
                "remote_shell_result", {"job_id": "does-not-exist"},
            ))
            self.assertEqual(missing["status"], "not_found")

            terminal = _payload_text(await session.call_tool(
                "remote_terminal_open", {"rows": 24, "cols": 80, "idle_timeout": 30},
            ))
            session_id = terminal["session_id"]
            await session.call_tool("remote_terminal_write", {
                "session_id": session_id, "data": "printf 'HELLO-MCP'; stty size\n",
            })
            collected = ""
            offset = 0
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and "HELLO-MCP" not in collected:
                chunk = _payload_text(await session.call_tool("remote_terminal_read", {
                    "session_id": session_id, "after": offset, "wait_seconds": 1,
                }))
                collected += chunk["data"]
                offset = chunk["next_offset"]
            self.assertIn("HELLO-MCP", collected)
            resized = _payload_text(await session.call_tool("remote_terminal_resize", {
                "session_id": session_id, "rows": 30, "cols": 100,
            }))
            self.assertEqual((resized["rows"], resized["cols"]), (30, 100))
            binary = _payload_text(await session.call_tool("remote_terminal_write", {
                "session_id": session_id,
                "data": base64.b64encode(b"stty size\n").decode(),
                "encoding": "base64",
            }))
            self.assertEqual(binary["status"], "written")
            closed = _payload_text(await session.call_tool(
                "remote_terminal_close", {"session_id": session_id},
            ))
            self.assertEqual(closed["status"], "closed")

            target = self.root / "remote.txt"
            written = _payload_text(await session.call_tool("remote_fs_write", {
                "path": str(target), "data": "alfa beta",
            }))
            self.assertEqual(written["status"], "written")
            self.assertTrue(target.exists())
            replaced = _payload_text(await session.call_tool("remote_fs_replace", {
                "path": str(target), "old": "beta", "new": "gamma",
            }))
            self.assertEqual(replaced["status"], "replaced")
            read = _payload_text(await session.call_tool("remote_fs_read", {
                "path": str(target), "offset": 0,
            }))
            self.assertEqual(read["data"], "alfa gamma")
            listing = _payload_text(await session.call_tool("remote_fs_list", {"path": str(self.root)}))
            self.assertIn("remote.txt", {entry["name"] for entry in listing["entries"]})
            found = _payload_text(await session.call_tool("remote_fs_find", {
                "path": str(self.root), "pattern": "*.txt",
            }))
            self.assertTrue(any(path.endswith("remote.txt") for path in found["results"]))
            searched = _payload_text(await session.call_tool("remote_fs_search", {
                "path": str(target), "query": "gamma",
            }))
            self.assertEqual(searched["results"][0]["line"], 1)
            info = _payload_text(await session.call_tool("remote_sysinfo", {}))
            self.assertEqual(info["uid"], os.getuid())

            local_source = self.root / "upload.bin"
            local_source.write_bytes(os.urandom(3000))
            uploaded = _payload_text(await session.call_tool("remote_upload", {
                "local_path": str(local_source), "remote_path": str(self.root / "up.bin"),
            }))
            self.assertEqual(uploaded["status"], "finished")
            self.assertEqual((self.root / "up.bin").read_bytes(), local_source.read_bytes())
            status = _payload_text(await session.call_tool(
                "remote_transfer_status", {"transfer_id": uploaded["transfer_id"]},
            ))
            self.assertEqual(status["status"], "finished")

            local_target = self.root / "down.bin"
            downloaded = _payload_text(await session.call_tool("remote_download", {
                "remote_path": str(self.root / "up.bin"), "local_path": str(local_target),
                "sync_timeout": 20,
            }))
            self.assertEqual(downloaded["status"], "finished")
            self.assertEqual(local_target.read_bytes(), local_source.read_bytes())
            self.assertEqual(
                _payload_text(await session.call_tool(
                    "remote_transfer_cancel", {"transfer_id": "nope"},
                ))["status"],
                "not_found",
            )

            missing_file = _payload_text(await session.call_tool("remote_fs_write", {
                "path": str(self.root / "oversized.txt"), "data": "y" * (64 * 1024 + 1),
            }))
            self.assertEqual(missing_file["status"], "error")
            self.assertEqual(missing_file["error"]["code"], "invalid_argument")
            self.assertFalse((self.root / "oversized.txt").exists())

            deleted = _payload_text(await session.call_tool("remote_fs_delete", {"path": str(target)}))
            self.assertEqual(deleted["status"], "deleted")
            bad = _payload_text(await session.call_tool("remote_fs_read", {
                "path": str(self.root / "absent"),
            }))
            self.assertEqual(bad["status"], "error")
            self.assertEqual(bad["error"]["code"], "not_found")
            self.assertFalse(bad["error"]["retryable"])
            self.assertTrue(bad["error"]["message"])
            self.assertLessEqual({"code", "message", "retryable"}, set(bad["error"]))
            self.assertIsInstance(bad["error"]["request_id"], int)

            stopped = _payload_text(await session.call_tool("slave_stop", {}))
            self.assertEqual(stopped["status"], "stopped")

        self._run(scenario)
        self.assertEqual(self.server_stderr, "")
        self.agent_process.wait(10)
        self.assertEqual(self.agent_process.returncode, 0)

    def test_restarted_daemon_reattaches_existing_agent(self) -> None:
        async def first_session(session: ClientSession) -> None:
            info = _payload_text(await session.call_tool("remote_sysinfo", {}))
            self.assertEqual(info["uid"], os.getuid())

        self._run(first_session)
        first_health = self._http_health()
        self.assertIsNotNone(first_health)
        first_pid = int(first_health["pid"])
        self._stop_http_server()
        self.assertEqual(
            (self.root / "state" / "active-link-id").read_text(encoding="ascii"),
            LINK,
        )

        async def resumed_session(session: ClientSession) -> None:
            deadline = time.monotonic() + 10
            result = {"status": "error"}
            while time.monotonic() < deadline:
                result = _payload_text(await session.call_tool(
                    "remote_shell", {"cmd": "printf reattached"},
                ))
                if result.get("status") == "finished":
                    break
                await asyncio.sleep(0.1)
            self.assertEqual(result["status"], "finished", result)
            self.assertEqual(result["stdout"], "reattached")
            status = _payload_text(await session.call_tool("kvm_status", {}))
            self.assertEqual(status["agent"]["status"], "ready")

        self._run(resumed_session)
        resumed_health = self._http_health()
        self.assertIsNotNone(resumed_health)
        self.assertNotEqual(int(resumed_health["pid"]), first_pid)

    def test_screen_returns_png_and_resolution_metadata_over_stdio(self) -> None:
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        png = (
            b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + (800).to_bytes(4, "big") + (600).to_bytes(4, "big")
        )
        ffmpeg = fake_bin / "ffmpeg"
        ffmpeg.write_text(
            "#!%s\nimport sys\nsys.stdout.buffer.write(bytes.fromhex(%r))\n"
            % (shutil.which("python3"), png.hex()),
            encoding="utf-8",
        )
        ffmpeg.chmod(0o700)
        self.server_env["PATH"] = str(fake_bin) + os.pathsep + self.server_env["PATH"]

        async def scenario(session: ClientSession) -> None:
            captured = await session.call_tool("screen", {})
            self.assertTrue(any(
                getattr(item, "type", None) == "image"
                and getattr(item, "mimeType", None) == "image/png"
                for item in captured.content
            ))
            metadata = _payload_text(captured)
            self.assertEqual(
                metadata["capture_resolution"],
                {"width_px": 800, "height_px": 600},
            )

        self._run(scenario)
        self.assertEqual(self.server_stderr, "")


if __name__ == "__main__":
    unittest.main()
