from __future__ import annotations

import base64
import errno
import hashlib
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

from titella.bootstrap import (
    _hid_container,
    _render_loader,
    loader_source,
    windows_hid_stage0_input,
)
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

# Inline fixture: the M suite must never read the slave checkout, and the
# staged snapshot template lives there until the bundle carries it.
STAGE0 = "$tok = '__HID_TOKEN__'\n$size = __HID_SIZE__\n$sha = '__HID_SHA256__'\n"
WINDOWS_TEMPLATE = ("P='__CDC_PORT__'\nF='__AGENT_FORMAT__'\n"
                   "H='__HID_TRANSPORT_SOURCE_B64__'\n")
UNIT = "namespace Titella.Slave {\n  public sealed class HidByteStream { }\n}\n"
BINDING = "HID:012345ABCDEF"


class HidStage0CommandTests(unittest.TestCase):
    def test_rejects_invalid_inputs(self) -> None:
        sha = hashlib.sha256(b"loader").hexdigest()
        cases = (
            ("", "0123456789abcdef", 10, sha),
            ("missing placeholders", "0123456789abcdef", 10, sha),
            (STAGE0, "0123456789abcde", 10, sha),
            (STAGE0, "0123456789abcdeg", 10, sha),
            (STAGE0, "0123456789abcdef", 0, sha),
            (STAGE0, "0123456789abcdef", 32769, sha),
            (STAGE0, "0123456789abcdef", True, sha),
            (STAGE0, "0123456789abcdef", 10, "0" * 63),
            (STAGE0, "0123456789abcdef", 10, "z" * 64),
        )
        for source, token, size, digest in cases:
            with self.subTest(token=token, size=size, digest=digest[:8]):
                with self.assertRaises(ValueError):
                    windows_hid_stage0_input(source, token, size, digest)

    def test_rejects_a_template_missing_one_placeholder(self) -> None:
        partial = STAGE0.replace("__HID_SHA256__", "fixed")
        with self.assertRaises(ValueError):
            windows_hid_stage0_input(partial, "0123456789abcdef", 10,
                                     hashlib.sha256(b"x").hexdigest())

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell is not installed")
    def test_launcher_accumulates_bounded_lines_until_the_terminator(self) -> None:
        """A single long line cannot cross a Windows console reader.

        Measured on Windows 11: a 2,980-character line reached
        ``[Console]::ReadLine()`` as 254 characters. The launcher therefore
        concatenates bounded lines and stops at ``END``.
        """
        source = STAGE0 + "[Console]::WriteLine('RESULT:' + $tok + ':' + $size + ':' + $sha)\n"
        digest = hashlib.sha256(b"loader").hexdigest()
        command, payload = windows_hid_stage0_input(source, "0123456789abcdef", 10, digest)
        self.assertIn("[Console]::ReadLine()", command)
        self.assertIn("-ceq 'END'", command)
        self.assertNotIn(payload, command)
        lines = [payload[index:index + 200] for index in range(0, len(payload), 200)] + ["END"]
        self.assertTrue(all(len(line) <= 200 for line in lines))
        result = subprocess.run(
            [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
            input="\n".join(lines) + "\n", text=True, capture_output=True, timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT:0123456789ABCDEF:10:" + digest, result.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell is not installed")
    def test_launcher_reassembles_the_payload_across_readline_boundaries(self) -> None:
        """The concatenation is what makes a >254-character input survive."""
        # Realistic template size: a short one gzips below the boundary and
        # would not exercise the reassembly at all.
        filler = "".join(f"$v{i} = {i}\n" for i in range(400))
        source = STAGE0 + filler + "[Console]::WriteLine('OK:' + $sha.Length)\n"
        digest = hashlib.sha256(b"loader").hexdigest()
        command, payload = windows_hid_stage0_input(source, "0123456789abcdef", 300, digest)
        self.assertGreater(len(payload), 254)
        lines = [payload[index:index + 200] for index in range(0, len(payload), 200)] + ["END"]
        result = subprocess.run(
            [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
            input="\n".join(lines) + "\n", text=True, capture_output=True, timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK:64", result.stdout)


class HidRenderTests(unittest.TestCase):
    def test_binding_and_source_are_coupled_in_both_directions(self) -> None:
        cases = (
            ("windows-dotnet", BINDING, None, "requires hid_transport_source"),
            ("windows-dotnet", "COM5", UNIT, "requires the HID"),
            # Any non-Windows implementation has no HID transport at all, so
            # a binding is refused for it by name rather than silently typed.
            ("python", BINDING, UNIT, "requires a windows implementation"),
            ("windows-dotnet", "HID:012345abcdef", UNIT, "12 uppercase"),
            ("windows-dotnet", BINDING, "  ", "non-empty"),
        )
        for implementation, port, source, message in cases:
            with self.subTest(port=port, implementation=implementation):
                with self.assertRaisesRegex(ValueError, message):
                    _render_loader(WINDOWS_TEMPLATE, implementation, LINK, SESSION, port,
                                   hid_transport_source=source)

    def test_each_windows_implementation_renders_its_own_literal(self) -> None:
        """The binding is a Python string for the Python loaders and a
        single-quoted PowerShell literal for the C# one."""
        # Each template only needs a port slot; the C# one additionally owes
        # the transport and agent-format placeholders its contract requires.
        py = _render_loader("P=__CDC_PORT__", "windows-python", LINK, SESSION,
                            BINDING, hid_transport_source=UNIT)
        self.assertIn(repr(BINDING), py)
        csharp_template = ("P='__CDC_PORT__'" + chr(10) + "F='__AGENT_FORMAT__'" + chr(10)
                           + "$s='__HID_TRANSPORT_SOURCE_B64__'")
        pwsh = _render_loader(csharp_template, "windows-dotnet", LINK, SESSION,
                              BINDING, hid_transport_source=UNIT)
        self.assertIn("'" + BINDING + "'", pwsh)
        # ...and it is rendered empty: the unit travels behind the marker.
        self.assertIn("$s=''", pwsh)

    def test_hid_source_is_rejected_when_the_template_carries_no_placeholder(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one HID transport placeholder"):
            _render_loader(
                "P='__CDC_PORT__'", "windows-dotnet", LINK, SESSION, BINDING,
                hid_transport_source=UNIT,
            )

    def test_each_dotnet_variant_renders_its_own_compile_mode(self) -> None:
        """The same template yields exe or dll from the selection alone."""
        template = ("P='__CDC_PORT__'\nF='__AGENT_FORMAT__'\n"
                    "H='__HID_TRANSPORT_SOURCE_B64__'\n")
        exe = _render_loader(template, "windows-dotnet", LINK, SESSION, BINDING,
                             hid_transport_source=UNIT)
        dll = _render_loader(template, "windows-dotnet-dll", LINK, SESSION, BINDING,
                             hid_transport_source=UNIT)
        self.assertIn("F='exe'", exe)
        self.assertIn("F='dll'", dll)
        self.assertNotIn("__AGENT_FORMAT__", exe + dll)
        # Everything else is identical: the mode is the only difference.
        self.assertEqual(exe.replace("F='exe'", "F='?'"),
                         dll.replace("F='dll'", "F='?'"))

    def test_agent_format_placeholder_is_required_exactly_once(self) -> None:
        for template, message in (
            ("P='__CDC_PORT__'\nH='__HID_TRANSPORT_SOURCE_B64__'\n",
             "exactly one agent format placeholder"),
            ("P='__CDC_PORT__'\nF='__AGENT_FORMAT__'\nF='__AGENT_FORMAT__'\n"
             "H='__HID_TRANSPORT_SOURCE_B64__'\n",
             "exactly one agent format placeholder"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    _render_loader(template, "windows-dotnet", LINK, SESSION, BINDING,
                                   hid_transport_source=UNIT)

    def test_python_loaders_are_not_given_the_agent_format(self) -> None:
        """The placeholder belongs to the C# template only; a Python loader
        that carried it would stay literally unrendered, which is a template
        defect and must not be papered over here."""
        rendered = _render_loader("P=__CDC_PORT__", "windows-python", LINK, SESSION,
                                  BINDING, hid_transport_source=UNIT)
        self.assertNotIn("__AGENT_FORMAT__", rendered)

    def test_hid_render_never_embeds_the_transport_unit(self) -> None:
        """Container mode: the placeholder is rendered empty and the unit
        travels only behind the marker, as raw UTF-8."""
        rendered = _render_loader(
            WINDOWS_TEMPLATE, "windows-dotnet", LINK, SESSION, BINDING,
            hid_transport_source=UNIT,
        )
        self.assertNotIn(UNIT, rendered)
        self.assertNotIn(base64.b64encode(UNIT.encode("utf-8")).decode("ascii"),
                         rendered)
        self.assertIn("''", rendered)

    def test_hid_container_carries_exactly_one_marker(self) -> None:
        rendered = _render_loader(
            WINDOWS_TEMPLATE, "windows-dotnet", LINK, SESSION, BINDING,
            hid_transport_source=UNIT,
        )
        container = _hid_container(rendered, UNIT)
        self.assertEqual(container.count("#@#"), 1)
        loader_part, _, unit_part = container.partition("#@#")
        self.assertEqual(unit_part.strip(), UNIT.strip())
        self.assertNotIn(UNIT, loader_part)

    def test_hid_container_rejects_a_marker_in_either_half(self) -> None:
        for loader_half, unit_half in (
            ("P='x'\n#@#", UNIT),
            (WINDOWS_TEMPLATE, "namespace X { }\n#@#"),
        ):
            with self.subTest(unit="#@#" in unit_half):
                with self.assertRaisesRegex(ValueError, "container marker"):
                    _hid_container(loader_half, unit_half)

    def test_empty_hid_source_is_valid_with_the_precompiled_transport(self) -> None:
        """A loader rendered with no embedded source is valid: stage-0
        precompiles the unit, and Open-Hid accepts an empty HidSrc when
        the type already exists."""
        rendered = _render_loader(
            WINDOWS_TEMPLATE, "windows-dotnet", LINK, SESSION, BINDING,
            hid_transport_source=UNIT,
        )
        self.assertIn("H=''", rendered)


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
