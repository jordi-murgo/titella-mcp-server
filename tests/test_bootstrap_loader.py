from __future__ import annotations

import hashlib
import errno
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from titella.bootstrap import loader_source
from titella_slave_assets.bundles import load_bundle
from titella_slave_assets.wire import (
    FIRMWARE_TO_SLAVE,
    SLAVE_TO_FIRMWARE,
    FrameParser,
    base_frame,
    data_b64,
    encode_frame,
)

LINK = "0123456789abcdef0123456789abcdef"
SESSION = "b" * 32


class LoaderHarness:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.parser = FrameParser(SLAVE_TO_FIRMWARE)
        self.pending: list[dict] = []

    def send(self, frame: dict) -> None:
        payload = encode_frame(frame, FIRMWARE_TO_SLAVE)
        total = 0
        while total < len(payload):
            total += os.write(self.fd, payload[total:])

    def next_frame(self, timeout: float = 10) -> dict:
        if self.pending:
            return self.pending.pop(0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.fd], [], [], max(0, deadline - time.monotonic()))
            if not readable:
                break
            try:
                chunk = os.read(self.fd, 4096)
            except OSError as exc:
                # Linux reports EIO while no process has the PTY slave open;
                # macOS blocks. The loader opens the named slave moments later.
                if exc.errno == errno.EIO:
                    time.sleep(0.01)
                    continue
                raise
            parsed = self.parser.feed(chunk)
            if parsed:
                self.pending.extend(parsed[1:])
                return parsed[0]
        raise TimeoutError("the loader did not send the expected frame")

    def expect(self, predicate, timeout: float = 10, ack: bool = True) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.next_frame(max(0.01, deadline - time.monotonic()))
            if predicate(frame):
                return frame
            if ack and frame["type"] == "data" and frame["channel"] != "bootstrap":
                self.send(base_frame(
                    LINK, frame["channel"], "ack",
                    session_id=frame["session_id"], ack_seq=frame["seq"],
                ))
        raise TimeoutError("the expected frame did not appear")


class BootstrapLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "loader.py"
        self.master, slave = pty.openpty()
        self.addCleanup(lambda: os.close(self.master) if self.master >= 0 else None)
        self.port = os.ttyname(slave)
        os.close(slave)
        # Wheel-only contract: the payload transferred to the loader is the
        # installed assets artifact, byte for byte, with its manifest hash.
        bundle = load_bundle("python")
        self.data = bundle.payload
        self.size = len(bundle.payload)
        self.sha256 = bundle.sha256
        self.process = None

    def _start(self, port: str | None = None) -> LoaderHarness:
        # The CDC override is rendered into the loader by the master; there is
        # no environment channel the loader could pick up instead.
        self.script.write_text(
            loader_source(LINK, SESSION, port or self.port), encoding="utf-8"
        )
        environment = dict(os.environ)
        environment["TMPDIR"] = str(self.root / "tmp")
        (self.root / "tmp").mkdir(exist_ok=True)
        runtime = shutil.which("python3.9") or sys.executable
        self.process = subprocess.Popen(
            [runtime, str(self.script)],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._stop)
        return LoaderHarness(self.master)

    def _stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.kill()
        if self.process:
            self.process.communicate(timeout=10)

    def test_loader_validates_hash_installs_zipapp_and_starts_agent(self) -> None:
        harness = self._start()
        ready = harness.expect(lambda f: f.get("op") == "loader.ready")
        self.assertEqual(ready["channel"], "bootstrap")
        self.assertEqual(ready["session_id"], SESSION)

        harness.send(base_frame(
            LINK, "bootstrap", "request", request_id=1, op="begin",
            body={"session_id": SESSION, "size": self.size, "sha256": self.sha256},
        ))
        acceptance = harness.expect(lambda f: f.get("request_id") == 1)
        self.assertEqual(acceptance["body"]["status"], "ready")

        for sequence, position in enumerate(range(0, len(self.data), 512)):
            harness.send(base_frame(
                LINK, "bootstrap", "data", session_id=SESSION, seq=sequence,
                data_b64=data_b64(self.data[position:position + 512]),
            ))
            ack = harness.expect(lambda f, s=sequence: f["type"] == "ack" and f["ack_seq"] == s)
            self.assertEqual(ack["session_id"], SESSION)

        harness.send(base_frame(
            LINK, "bootstrap", "request", request_id=2, op="commit",
            body={"session_id": SESSION},
        ))
        committed = harness.expect(lambda f: f.get("request_id") == 2)
        self.assertEqual(committed["body"]["status"], "started")
        self.assertEqual(committed["body"]["sha256"], self.sha256)
        self.process.wait(10)
        self.assertEqual(self.process.returncode, 0)

        installed = [path for path in (self.root / "tmp").rglob(".payload.pyz")]
        self.assertEqual(len(installed), 1)
        self.assertEqual(hashlib.sha256(installed[0].read_bytes()).hexdigest(), self.sha256)
        self.assertEqual(installed[0].stat().st_mode & 0o777, 0o700)
        self.assertEqual(installed[0].parent.stat().st_mode & 0o777, 0o700)

        hello = harness.expect(lambda f: f["channel"] == "control" and f["type"] == "hello", timeout=15)
        self.assertEqual(hello["link_id"], LINK)
        self.assertIn("python", hello["body"])
        harness.send(base_frame(
            LINK, "control", "request", request_id=3, op="bind",
            body={"link_id": LINK},
        ))
        bound = harness.expect(lambda f: f.get("request_id") == 3)
        self.assertEqual(bound["body"]["status"], "ready")

        harness.send(base_frame(LINK, "control", "request", request_id=4, op="agent.stop", body={}))
        harness.expect(lambda f: f.get("request_id") == 4)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and installed[0].exists():
            time.sleep(0.05)
        self.assertFalse(installed[0].exists(), "the payload must be removed on stop")

    def test_checksum_mismatch_reports_error_and_removes_partial(self) -> None:
        harness = self._start()
        harness.expect(lambda f: f.get("op") == "loader.ready")
        harness.send(base_frame(
            LINK, "bootstrap", "request", request_id=1, op="begin",
            body={"session_id": SESSION, "size": len(self.data), "sha256": "0" * 64},
        ))
        harness.expect(lambda f: f.get("request_id") == 1)
        for sequence, position in enumerate(range(0, len(self.data), 512)):
            harness.send(base_frame(
                LINK, "bootstrap", "data", session_id=SESSION, seq=sequence,
                data_b64=data_b64(self.data[position:position + 512]),
            ))
            harness.expect(lambda f, s=sequence: f["type"] == "ack" and f["ack_seq"] == s)
        harness.send(base_frame(
            LINK, "bootstrap", "request", request_id=2, op="commit",
            body={"session_id": SESSION},
        ))
        error = harness.expect(lambda f: f["type"] == "error" and f["channel"] == "bootstrap")
        self.assertEqual(error["body"]["code"], "checksum_mismatch")
        # With the commit request in flight the error must be correlatable.
        self.assertEqual(error["request_id"], 2)
        self.assertEqual(error["op"], "commit")
        self.process.wait(10)
        self.assertEqual(self.process.returncode, 1)
        self.assertEqual(
            list((self.root / "tmp").rglob("*")), [], "no partial file or directory may remain",
        )

    def test_loader_source_probes_linux_cdc_ports(self) -> None:
        # Linux's native CDC shows up as ttyACM*; the loader must probe it
        # in addition to the macOS ports.
        self.assertIn("/dev/ttyACM*", loader_source(LINK, SESSION))

    def test_cdc_absent_reports_structured_error_before_any_transfer(self) -> None:
        # Without a CDC there is no channel: the loader must fail on stderr before transferring.
        self._start(port=str(self.root / "missing-cdc"))
        stderr = self.process.communicate(timeout=20)[1].decode()
        self.assertEqual(self.process.returncode, 1)
        self.assertEqual(stderr.strip(), "cdc_not_found")
        self.assertEqual(
            list((self.root / "tmp").rglob("*")), [], "the bootstrap directory must not be created",
        )


if __name__ == "__main__":
    unittest.main()
