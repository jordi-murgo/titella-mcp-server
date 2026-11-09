from __future__ import annotations

import concurrent.futures
import errno
import os
import pty
import select
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from titella.config import load_config
from titella.serial_link import SerialLink, TransportError
from titella_slave_assets.wire import (
    FIRMWARE_TO_MASTER,
    MASTER_TO_FIRMWARE,
    FrameParser,
    base_frame,
    encode_frame,
)


class PtyPeer:
    def __init__(self, master: int, link_id: str) -> None:
        self.master = master
        self.link_id = link_id
        self.parser = FrameParser(MASTER_TO_FIRMWARE)
        self.frames: list[dict] = []

    def read_until(self, predicate, timeout: float = 3) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in list(self.frames):
                if predicate(frame):
                    self.frames.remove(frame)
                    return frame
            readable, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not readable:
                break
            try:
                chunk = os.read(self.master, 4096)
            except OSError as exc:
                # A replaced Linux PTY has no slave open for a short interval
                # and reports EIO; the reconnect loop opens the new path.
                if exc.errno == errno.EIO:
                    time.sleep(0.01)
                    continue
                raise
            self.frames.extend(self.parser.feed(chunk))
        raise TimeoutError("peer did not receive expected frame")

    def respond(self, request: dict, body: dict, fragments: bool = False) -> None:
        response = base_frame(
            self.link_id,
            request["channel"],
            "response",
            request_id=request["request_id"],
            op=request["op"],
            body=body,
        )
        payload = encode_frame(response, FIRMWARE_TO_MASTER)
        if fragments:
            for byte in payload:
                os.write(self.master, bytes((byte,)))
        else:
            os.write(self.master, payload)


class SerialLinkIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.master, slave = pty.openpty()
        self.addCleanup(lambda: os.close(self.master) if self.master >= 0 else None)
        self.port = Path(self.temp.name) / "serial"
        self.port.symlink_to(os.ttyname(slave))
        os.close(slave)
        config = load_config(
            str(Path(self.temp.name) / "absent.toml"),
            {
                "TITELLA_STATE_DIR": str(Path(self.temp.name) / "state"),
                "TITELLA_SERIAL_PORT": str(self.port),
                "TITELLA_HEARTBEAT_S": "1",
                "TITELLA_DISCONNECT_GRACE_S": "3",
            },
        )
        self.link = SerialLink(replace(config, ack_timeout_s=0.05))
        self.addCleanup(self.link.close)
        self.assertTrue(self.link.wait_connected(3))
        self.peer = PtyPeer(self.master, self.link.link_id)

    def test_concurrent_requests_are_correlated_out_of_order(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.link.request, "hid", "first", {"value": 1}, 3)
            second = pool.submit(self.link.request, "hid", "second", {"value": 2}, 3)
            requests = [
                self.peer.read_until(lambda frame: frame["type"] == "request"),
                self.peer.read_until(lambda frame: frame["type"] == "request"),
            ]
            by_op = {frame["op"]: frame for frame in requests}
            os.write(self.master, b"ESP-ROM banner\n")
            self.peer.respond(by_op["second"], {"value": "B"}, fragments=True)
            self.peer.respond(by_op["first"], {"value": "A"})
            self.assertEqual(first.result()["body"], {"value": "A"})
            self.assertEqual(second.result()["body"], {"value": "B"})

    def test_heartbeat_uses_control_priority_without_a_request(self) -> None:
        heartbeat = self.peer.read_until(
            lambda frame: frame["channel"] == "control" and frame["type"] == "heartbeat",
            timeout=2,
        )
        self.assertEqual(heartbeat["link_id"], self.link.link_id)

    def test_reopens_replaced_port_and_keeps_link_id(self) -> None:
        states: list[str] = []
        unsubscribe = self.link.subscribe_state(states.append)
        self.addCleanup(unsubscribe)
        old_link_id = self.link.link_id
        os.close(self.master)
        self.master = -1
        new_master, new_slave = pty.openpty()
        target = os.ttyname(new_slave)
        os.close(new_slave)
        self.port.unlink()
        self.port.symlink_to(target)
        self.master = new_master
        self.peer = PtyPeer(new_master, old_link_id)

        result: dict = {}
        failure: list[BaseException] = []

        def request() -> None:
            try:
                result.update(self.link.request("hid", "health", {}, timeout=5)["body"])
            except BaseException as exc:
                failure.append(exc)

        worker = threading.Thread(target=request)
        worker.start()
        frame = self.peer.read_until(lambda item: item.get("op") == "health", timeout=5)
        self.peer.respond(frame, {"ok": True, "status": "hid_ready"})
        worker.join(5)
        self.assertFalse(failure)
        self.assertEqual(result["status"], "hid_ready")
        self.assertEqual(self.link.link_id, old_link_id)
        self.assertIn("reconnecting", states)
        self.assertEqual(states[-1], "connected")

    def test_inflight_hid_request_fails_when_transport_disappears(self) -> None:
        failure: list[BaseException] = []

        def request() -> None:
            try:
                self.link.request("hid", "mouse_rel", {"x": 1, "y": 1}, timeout=5)
            except BaseException as exc:
                failure.append(exc)

        worker = threading.Thread(target=request)
        worker.start()
        self.peer.read_until(lambda frame: frame.get("op") == "mouse_rel")
        os.close(self.master)
        self.master = -1
        worker.join(3)
        self.assertEqual(len(failure), 1)
        self.assertIsInstance(failure[0], TransportError)
        self.assertEqual(failure[0].code, "transport_disconnected")

    def test_clean_shutdown_removes_resume_identifier(self) -> None:
        path = self.link.link_path
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertRegex(path.read_text(), r"^[0-9a-f]{32}$")
        self.link.close()
        self.assertFalse(path.exists())


class TcpPeer:
    """Simulated firmware: accepts a TCP client and speaks the same framing."""

    def __init__(self, listener: "socket.socket", link_id: str) -> None:
        self.listener = listener
        self.link_id = link_id
        self.parser = FrameParser(MASTER_TO_FIRMWARE)
        self.frames: list[dict] = []
        self.connection: "socket.socket | None" = None

    def accept(self, timeout: float = 3) -> None:
        self.listener.settimeout(timeout)
        self.connection, _ = self.listener.accept()
        self.connection.settimeout(3)

    def read_until(self, predicate, timeout: float = 3) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in list(self.frames):
                if predicate(frame):
                    self.frames.remove(frame)
                    return frame
            assert self.connection is not None
            self.connection.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                chunk = self.connection.recv(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            self.frames.extend(self.parser.feed(chunk))
        raise TimeoutError("peer did not receive expected frame")

    def respond(self, request: dict, body: dict) -> None:
        assert self.connection is not None
        response = base_frame(
            self.link_id,
            request["channel"],
            "response",
            request_id=request["request_id"],
            op=request["op"],
            body=body,
        )
        self.connection.sendall(encode_frame(response, FIRMWARE_TO_MASTER))

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class SerialLinkWifiTests(unittest.TestCase):
    """Same link over TCP: correlation, reconnection and transport cut-off."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.addCleanup(self.listener.close)
        host, port = self.listener.getsockname()
        config = load_config(
            str(Path(self.temp.name) / "absent.toml"),
            {
                "TITELLA_STATE_DIR": str(Path(self.temp.name) / "state"),
                "TITELLA_TRANSPORT": "wifi",
                "TITELLA_WIFI_HOST": host,
                "TITELLA_WIFI_PORT": str(port),
                "TITELLA_HEARTBEAT_S": "1",
                "TITELLA_DISCONNECT_GRACE_S": "3",
            },
        )
        self.link = SerialLink(replace(config, ack_timeout_s=0.05))
        self.addCleanup(self.link.close)
        self.peer = TcpPeer(self.listener, self.link.link_id)
        self.thread = threading.Thread(target=self.peer.accept, daemon=True)
        self.thread.start()
        self.assertTrue(self.link.wait_connected(3))
        self.thread.join(3)
        self.addCleanup(self.peer.close)

    def test_requests_are_correlated_over_tcp(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.link.request, "hid", "first", {"value": 1}, 3)
            second = pool.submit(self.link.request, "hid", "second", {"value": 2}, 3)
            requests = [
                self.peer.read_until(lambda frame: frame["type"] == "request"),
                self.peer.read_until(lambda frame: frame["type"] == "request"),
            ]
            by_op = {frame["op"]: frame for frame in requests}
            self.peer.respond(by_op["second"], {"value": "B"})
            self.peer.respond(by_op["first"], {"value": "A"})
            self.assertEqual(first.result()["body"], {"value": "A"})
            self.assertEqual(second.result()["body"], {"value": "B"})

    def test_reconnects_after_the_firmware_replaces_the_client(self) -> None:
        states: list[str] = []
        unsubscribe = self.link.subscribe_state(states.append)
        self.addCleanup(unsubscribe)
        old_link_id = self.link.link_id
        self.peer.close()
        # The ESP32 replaces the client: the master reconnects on its own.
        self.thread = threading.Thread(target=self.peer.accept, daemon=True)
        self.thread.start()
        self.thread.join(5)
        result: dict = {}
        failure: list[BaseException] = []

        def request() -> None:
            try:
                result.update(self.link.request("hid", "health", {}, timeout=5)["body"])
            except BaseException as exc:
                failure.append(exc)

        worker = threading.Thread(target=request)
        worker.start()
        frame = self.peer.read_until(lambda item: item.get("op") == "health", timeout=5)
        self.peer.respond(frame, {"ok": True, "status": "hid_ready"})
        worker.join(5)
        self.assertFalse(failure)
        self.assertEqual(result["status"], "hid_ready")
        self.assertEqual(self.link.link_id, old_link_id)
        self.assertIn("reconnecting", states)
        self.assertEqual(states[-1], "connected")

    def test_inflight_request_fails_when_tcp_peer_disappears(self) -> None:
        failure: list[BaseException] = []

        def request() -> None:
            try:
                self.link.request("hid", "mouse_rel", {"x": 1, "y": 1}, timeout=5)
            except BaseException as exc:
                failure.append(exc)

        worker = threading.Thread(target=request)
        worker.start()
        self.peer.read_until(lambda frame: frame.get("op") == "mouse_rel")
        self.peer.close()
        worker.join(3)
        self.assertEqual(len(failure), 1)
        self.assertIsInstance(failure[0], TransportError)
        self.assertEqual(failure[0].code, "transport_disconnected")

    def test_port_label_reports_the_tcp_endpoint(self) -> None:
        self.assertEqual(self.link.port, "%s:%s" % self.listener.getsockname())
        self.assertEqual(self.link.transport, "wifi")


if __name__ == "__main__":
    unittest.main()
