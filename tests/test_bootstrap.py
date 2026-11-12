from __future__ import annotations

import base64
import gzip
import json
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from titella.bootstrap import (
    PLATFORM_PROFILES,
    BootstrapManager,
    loader_command,
    loader_source,
    posix_loader_blob,
    posix_stub_command,
)
from titella.config import load_config
from titella.serial_link import TransportError
from titella_slave_assets.bundles import load_bundle

LINK = "0123456789abcdef0123456789abcdef"


class FakeLease:
    def __init__(self) -> None:
        self.owners: list[str] = []

    def require(self, owner: str) -> None:
        self.owners.append(owner)


class FakeHid:
    def __init__(self) -> None:
        self.lease = FakeLease()
        self.layouts: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str, tuple[str, ...]]] = []
        self.typed: list[str] = []
        self.typed_delays: list[int] = []
        self.events: list[tuple[str, object, object]] = []
        self.on_enter = None

    def keyboard_layout(self, owner, layout):
        self.layouts.append((owner, layout))
        return {"ok": True}

    def key(self, owner, key, mods):
        self.keys.append((owner, key, tuple(mods)))
        self.events.append(("key", key, tuple(mods)))
        if key == "ENTER" and self.on_enter:
            callback, self.on_enter = self.on_enter, None
            callback()
        return {"ok": True}

    def type_text(self, owner, text, delay):
        self.typed.append(text)
        self.typed_delays.append(delay)
        self.events.append(("type", text, delay))
        return {"ok": True}


class FakeLink:
    def __init__(self) -> None:
        self.link_id = LINK
        self.handlers: dict[tuple[str, str | None], list] = {}
        self.payload = bytearray()
        self.requests: list[tuple[str, str, dict]] = []
        self.transport = "serial"
        # Fresh-health answer for the HID bootstrap. The default mirrors a
        # 1.6.0 firmware; a test overrides it to prove the old-chip rejection.
        self.health: dict = {
            "firmware": "1.6.0", "protocol": 1, "uart_baud": 2000000,
            "slave_transports": ["cdc", "hid"], "slave_transport": "cdc",
            "hid_data_protocol": 1, "usb_vid": 6127, "usb_pid": 41871,
            "usb_serial": "012345ABCDEF",
        }
        self.stage_failures: dict[str, str] = {}
        self.staged_bytes = bytearray()
        # The firmware's read-only view of the selected destination. Only this
        # proves a close; RPC failures never do.
        self.selected_transport = "cdc"
        self.slave_connected = False
        self.stop_reply: str | None = None  # error code to raise for agent.stop
        # False models an agent that answers the stop but keeps its stream: the
        # reply alone must never be trusted as a close.
        self.stop_releases = True

    def subscribe(self, channel, handler, session_id=None):
        key = (channel, session_id)
        self.handlers.setdefault(key, []).append(handler)
        def unsubscribe():
            self.handlers[key].remove(handler)
        return unsubscribe

    def emit(self, frame):
        session_id = frame.get("session_id")
        for key in ((frame["channel"], session_id), (frame["channel"], None)):
            for handler in list(self.handlers.get(key, [])):
                handler(frame)

    def request(self, channel, op, body, timeout=10):
        self.requests.append((channel, op, body))
        if channel == "hid" and op == "health":
            return {"body": dict(
                self.health,
                slave_transport=self.selected_transport,
                slave_connected=self.slave_connected,
            )}
        if channel == "hid" and op in {"loader_stage_begin", "loader_stage_chunk",
                                       "loader_stage_commit", "loader_stage_clear"}:
            failure = self.stage_failures.get(op)
            if failure is not None:
                raise TransportError(failure, f"{op} failed", False)
            if op == "loader_stage_begin":
                self.staged_bytes = bytearray()
                self.stage_size = body["size"]
                return {"body": {"ok": True, "staged": 0}}
            if op == "loader_stage_chunk":
                chunk = base64.b64decode(body["data_b64"])
                self.staged_bytes.extend(chunk)
                return {"body": {"ok": True, "staged": len(self.staged_bytes)}}
            return {"body": {"ok": True}}
        if channel == "bootstrap" and op == "loader.push":
            self.staged = getattr(self, "staged", 0) + len(body["data_b64"])
            return {"body": {"status": "staged", "staged": self.staged,
                             "total": body["total"]}}
        if channel == "bootstrap" and op == "commit":
            # The agent now owns the selected destination.
            self.slave_connected = True
            self.emit({
                "v": 1, "channel": "control", "type": "hello", "link_id": LINK,
                "body": {"uid": 501, "python": "3.9.18"},
            })
            return {"body": {"status": "started"}}
        if channel == "control" and op == "bind":
            if not self.slave_connected:
                raise TransportError("route_error", "destination is not connected", False)
            return {"body": {"status": "ready", "link_id": LINK}}
        if channel == "control" and op == "agent.stop":
            if self.stop_reply is not None:
                raise TransportError(self.stop_reply, "stop did not answer", True)
            if not self.slave_connected:
                raise TransportError("route_error", "destination is not connected", False)
            # The agent answers and then closes its stream; the fake applies
            # that close so the observable state changes exactly once.
            if self.stop_releases:
                self.slave_connected = False
            return {"body": {"status": "stopped"}}
        if channel == "hid" and op == "slave_transport":
            self.selected_transport = body["transport"]
            return {"body": {"ok": True, "transport": body["transport"],
                             "status": "selected"}}
        return {"body": {"status": "ready"}}

    def send_data(self, channel, session_id, sequence, data):
        self.payload.extend(data)


class BootstrapTests(unittest.TestCase):
    def test_single_file_bootstrap_selects_layout_chunks_command_and_binds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
            })
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)

            def loader_ready():
                session_id = manager.status()["session_id"]
                link.emit({
                    "v": 1, "channel": "bootstrap", "type": "event", "link_id": LINK,
                    "session_id": session_id, "op": "loader.ready", "body": {"python": "3.9.18"},
                })

            # First Enter opens Terminal; the second runs the loader.
            enters = 0
            original_key = hid.key
            def key(owner, name, mods):
                nonlocal enters
                result = original_key(owner, name, mods)
                if name == "ENTER":
                    enters += 1
                    if enters == 2:
                        loader_ready()
                return result
            hid.key = key

            result = manager.bootstrap("operator", "en_US", platform="macos",
                                       layout_confirmed=True)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["platform"], "macos")
            self.assertEqual(result["implementation"], "python")
            self.assertEqual(hid.lease.owners, ["operator"])
            self.assertEqual(hid.layouts, [("operator", "en_US")])
            self.assertEqual(hid.keys[0], ("operator", "SPACE", ("CMD",)))
            self.assertTrue(all(len(chunk) <= 256 for chunk in hid.typed[1:]))
            # The transferred payload is the immutable wheel artifact; no
            # runtime build and no agent-cache directory may exist.
            bundle = load_bundle("python")
            self.assertEqual(bytes(link.payload), bundle.payload)
            self.assertEqual(result["sha256"], bundle.sha256)
            self.assertFalse(
                (Path(temp) / "agent-cache").exists(),
                "the master must not compile or cache the payload on disk",
            )
            self.assertIn(("control", "bind", {"link_id": LINK}), link.requests)
            status = manager.status()
            self.assertEqual(status["platform"], "macos")
            self.assertEqual(status["implementation"], "python")
            self.assertEqual(manager.stop()["status"], "stopped")

    def test_loader_error_before_ready_surfaces_real_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
            })
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)

            def loader_error():
                link.emit({
                    "v": 1, "channel": "bootstrap", "type": "error", "link_id": LINK,
                    "session_id": manager.status()["session_id"],
                    "body": {"code": "cdc_not_found", "message": "missing cdc", "retryable": False},
                })

            # First Enter opens Terminal; the second runs the loader, which
            # fails before loader.ready.
            enters = 0
            original_key = hid.key
            def key(owner, name, mods):
                nonlocal enters
                result = original_key(owner, name, mods)
                if name == "ENTER":
                    enters += 1
                    if enters == 2:
                        loader_error()
                return result
            hid.key = key

            result = manager.bootstrap("operator", "en_US", platform="macos",
                                       layout_confirmed=True)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "cdc_not_found")
            self.assertEqual(result["error"]["message"], "missing cdc")
            self.assertFalse(result["error"]["retryable"])
            self.assertEqual(result["error"]["session_id"], result["session_id"])

    def _auto_ready(self, link, manager, hid):
        """Emits loader.ready when the loader command or stub is submitted.

        The hook runs on ENTER boundaries, so it works for the macOS
        Spotlight, the Linux direct binding and the Windows CDC stub without
        fixing a brittle Enter count.
        """
        original_key = hid.key
        marker = {"index": 0}

        def looks_like_loader(text: str) -> bool:
            return "python3 -c" in text or "stage.ready" in text

        def key(owner, name, mods):
            result = original_key(owner, name, mods)
            if name == "ENTER":
                submitted = "".join(hid.typed[marker["index"]:])
                marker["index"] = len(hid.typed)
                if looks_like_loader(submitted):
                    link.emit({
                        "v": 1, "channel": "bootstrap", "type": "event", "link_id": LINK,
                        "session_id": manager.status()["session_id"],
                        "op": "loader.ready", "body": {"runtime": "test"},
                    })
            return result
        hid.key = key

    def test_bootstrap_stops_a_previous_agent_even_when_the_gate_is_closed(self) -> None:
        """A live agent with a stale-closed gate still owns the CDC.

        Gating the stop on the in-memory gate left that agent running for ever
        and every later bootstrap died as bootstrap_loader_timeout, which reads
        as a port problem the operator cannot see.
        """
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
                "TITELLA_SLAVE_PLATFORM": "windows",
                "TITELLA_SLAVE_CDC_PORT": "COM5",
            })
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            self._auto_ready(link, manager, hid)
            # The agent process is alive, but the gate closed on a missed
            # heartbeat: bootstrap must still send the stop before typing.
            self.assertFalse(manager.gate.ready)

            result = manager.bootstrap("operator", "en_US", layout_confirmed=True)

            self.assertEqual(result["status"], "ready")
            stops = [r for r in link.requests if r[1] == "agent.stop"]
            self.assertEqual(len(stops), 1, "the previous agent is stopped unconditionally")
            self.assertEqual(link.requests[0][1], "agent.stop",
                             "the stop precedes any bootstrap traffic")

    def test_bootstrap_continues_when_no_previous_agent_answers(self) -> None:
        """An absent agent answers route_error; that is not a bootstrap failure."""
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
                "TITELLA_SLAVE_PLATFORM": "windows",
                "TITELLA_SLAVE_CDC_PORT": "COM5",
            })
            link = FakeLink()

            def refuse(channel, op, body, timeout=10):
                if channel == "control" and op == "agent.stop":
                    link.requests.append((channel, op, body))
                    raise TransportError("route_error", "no agent route", False)
                return FakeLink.request(link, channel, op, body, timeout)

            link.request = refuse  # type: ignore[assignment]
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            self._auto_ready(link, manager, hid)

            result = manager.bootstrap("operator", "en_US", layout_confirmed=True)

            self.assertEqual(result["status"], "ready", result)

    def test_loader_timeout_names_the_occupied_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
                "TITELLA_SLAVE_PLATFORM": "windows",
                "TITELLA_SLAVE_CDC_PORT": "COM5",
            })
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            # The wait is shortened so the timeout path runs without sleeping
            # the production deadline; the diagnostic itself is unchanged.
            saved = dict(PLATFORM_PROFILES)
            try:
                for name, profile in saved.items():
                    PLATFORM_PROFILES[name] = replace(profile, loader_wait_s=0.05)
                result = manager.bootstrap("operator", "en_US", layout_confirmed=True)
            finally:
                PLATFORM_PROFILES.clear()
                PLATFORM_PROFILES.update(saved)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "bootstrap_loader_timeout")
            # A timeout must name the port and the usual cause instead of
            # leaving the operator with an unexplained stub failure.
            self.assertIn("COM5", result["error"]["message"])
            self.assertIn("previous agent", result["error"]["message"])

    def test_missing_platform_fails_before_any_keystroke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
            })
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            result = manager.bootstrap("operator")
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "slave_platform_required")
            self.assertFalse(result["error"]["retryable"])
            # No layout request, no shortcut and no text reached the target,
            # and a rejected call must not overwrite the previous state.
            self.assertEqual(hid.keys, [])
            self.assertEqual(hid.typed, [])
            self.assertEqual(manager.status()["status"], "not_started")
            self.assertEqual(link.requests, [])

    def test_missing_layout_rejects_even_with_a_configured_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
                "TITELLA_KEYBOARD_LAYOUT": "en_US",
                "TITELLA_SLAVE_PLATFORM": "macos",
            })
            for platform in (None, "macos", "linux", "windows"):
                with self.subTest(platform=platform):
                    link = FakeLink()
                    hid = FakeHid()
                    manager = BootstrapManager(config, link, hid)
                    result = manager.bootstrap("operator", platform=platform)
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error"]["code"], "slave_layout_required")
                    self.assertFalse(result["error"]["retryable"])
                    # The configured default is not a bootstrap fallback: no
                    # layout request, no lease, no keystroke, no staging.
                    self.assertEqual(hid.layouts, [])
                    self.assertEqual(hid.lease.owners, [])
                    self.assertEqual(hid.keys, [])
                    self.assertEqual(hid.typed, [])
                    self.assertEqual(link.requests, [])
                    self.assertEqual(link.handlers, {})
                    self.assertEqual(manager.status()["status"], "not_started")

    def test_unconfirmed_layout_rejects_before_any_keystroke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
            })
            # Absent (default False), explicit False and truthy non-booleans
            # must all be rejected: only a real True attests the verification.
            confirmations = (None, False, "true", "yes", 1)
            for confirmation in confirmations:
                with self.subTest(layout_confirmed=confirmation):
                    link = FakeLink()
                    hid = FakeHid()
                    manager = BootstrapManager(config, link, hid)
                    call = {"owner": "operator", "layout": "es_ES", "platform": "macos"}
                    if confirmation is not None:
                        call["layout_confirmed"] = confirmation
                    result = manager.bootstrap(**call)
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error"]["code"], "slave_layout_unconfirmed")
                    self.assertFalse(result["error"]["retryable"])
                    self.assertEqual(hid.layouts, [])
                    self.assertEqual(hid.lease.owners, [])
                    self.assertEqual(hid.keys, [])
                    self.assertEqual(hid.typed, [])
                    self.assertEqual(link.requests, [])
                    self.assertEqual(link.handlers, {})
                    self.assertEqual(manager.status()["status"], "not_started")

    def test_unsupported_layout_rejects_before_any_keystroke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
            })
            for layout in ("fr_FR", "es-ES", "EN_US", ""):
                with self.subTest(layout=layout):
                    link = FakeLink()
                    hid = FakeHid()
                    manager = BootstrapManager(config, link, hid)
                    result = manager.bootstrap(
                        "operator", layout, platform="macos", layout_confirmed=True,
                    )
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error"]["code"], "invalid_argument")
                    self.assertFalse(result["error"]["retryable"])
                    self.assertEqual(hid.layouts, [])
                    self.assertEqual(hid.lease.owners, [])
                    self.assertEqual(hid.keys, [])
                    self.assertEqual(hid.typed, [])
                    self.assertEqual(link.requests, [])
                    self.assertEqual(manager.status()["status"], "not_started")

    def test_confirmed_layouts_reach_the_handshake_on_every_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for platform in ("macos", "linux", "windows"):
                environ = {
                    "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                    "TITELLA_STATE_DIR": temp,
                }
                if platform == "linux":
                    environ["TITELLA_TERMINAL_MODS"] = "SUPER"
                    environ["TITELLA_TERMINAL_KEY"] = "RETURN"
                config = load_config(environ=environ)
                for layout in ("es_ES", "en_US"):
                    with self.subTest(platform=platform, layout=layout):
                        link = FakeLink()
                        hid = FakeHid()
                        manager = BootstrapManager(config, link, hid)
                        self._auto_ready(link, manager, hid)
                        result = manager.bootstrap(
                            "operator", layout, platform=platform,
                            layout_confirmed=True,
                        )
                        self.assertEqual(result["status"], "ready", result)
                        self.assertEqual(result["platform"], platform)
                        # The verified layout is what reached the HID layer.
                        self.assertEqual(hid.layouts, [("operator", layout)])
                        self.assertEqual(hid.lease.owners, ["operator"])
                        self.assertIn(
                            ("control", "bind", {"link_id": LINK}), link.requests,
                        )

    def test_configured_platform_is_used_when_the_argument_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
                "TITELLA_SLAVE_PLATFORM": "macos",
            })
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            self._auto_ready(link, manager, hid)
            result = manager.bootstrap(owner="operator", layout="en_US",
                                       layout_confirmed=True)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["platform"], "macos")
            self.assertEqual(hid.keys[0], ("operator", "SPACE", ("CMD",)))

    def test_invalid_platform_and_port_fail_before_any_keystroke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
            })
            for platform in ("freebsd", "Linux", ""):
                with self.subTest(platform=platform):
                    hid = FakeHid()
                    manager = BootstrapManager(config, FakeLink(), hid)
                    result = manager.bootstrap("operator", platform=platform)
                    self.assertEqual(result["error"]["code"], "invalid_argument")
                    self.assertEqual(hid.keys, [])
                    self.assertEqual(hid.typed, [])
            hid = FakeHid()
            manager = BootstrapManager(config, FakeLink(), hid)
            for port in ("com7", "COM0", "/dev/ttyACM0"):
                with self.subTest(port=port):
                    result = manager.bootstrap(
                        "operator", "en_US", platform="windows", cdc_port=port,
                        layout_confirmed=True,
                    )
                    self.assertEqual(result["error"]["code"], "invalid_argument")
            self.assertEqual(hid.keys, [])

    def test_linux_without_a_configured_binding_fails_before_hid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
            })
            hid = FakeHid()
            manager = BootstrapManager(config, FakeLink(), hid)
            result = manager.bootstrap("operator", "en_US", platform="linux",
                                       layout_confirmed=True)
            self.assertEqual(result["error"]["code"], "slave_launcher_required")
            self.assertEqual(hid.keys, [])
            self.assertEqual(hid.typed, [])

    def test_linux_uses_the_configured_binding_and_the_zipapp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
                "TITELLA_TERMINAL_MODS": "SUPER",
                "TITELLA_TERMINAL_KEY": "RETURN",
            })
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            self._auto_ready(link, manager, hid)
            result = manager.bootstrap("operator", "en_US", platform="linux",
                                       layout_confirmed=True)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["platform"], "linux")
            self.assertEqual(result["implementation"], "python")
            self.assertEqual(hid.keys[0], ("operator", "RETURN", ("SUPER",)))
            # The Linux profile types no launcher text: the shortcut already
            # opened the terminal, so only the loader command is typed.
            self.assertTrue(hid.typed[0].startswith("python3 -c "))
            bundle = load_bundle("python")
            self.assertEqual(bytes(link.payload), bundle.payload)


    def test_empty_launcher_text_override_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
                "TITELLA_SLAVE_PLATFORM": "macos",
                "TITELLA_LAUNCHER_TEXT": "",
            })
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            self._auto_ready(link, manager, hid)
            manager.bootstrap("operator", "en_US", layout_confirmed=True)
            # The platform default "Terminal" must not replace an explicit empty
            # value; only the loader command reaches the target.
            self.assertFalse(any("Terminal" in chunk for chunk in hid.typed))

    def test_cdc_port_override_is_rendered_into_the_python_stub(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
            })
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            self._auto_ready(link, manager, hid)
            manager.bootstrap("operator", "en_US", platform="macos",
                              cdc_port="/dev/cu.usbmodemTEST", layout_confirmed=True)
            # The command is typed in bounded chunks: reassemble them first.
            command = "".join(hid.typed[1:])
            encoded = command.split('b64decode("', 1)[1].rsplit('"', 1)[0]
            # The stub is gzip+base64: only its own ~1 200 characters travel
            # over HID; the loader arrives over the CDC with flow control.
            stub = gzip.decompress(base64.b64decode(encoded)).decode("utf-8")
            self.assertIn("P='/dev/cu.usbmodemTEST'", stub)
            self.assertIn("stage.ready", stub)
            self.assertIn("loader.push", stub)
            self.assertNotIn("TITELLA_CDC_PORT", stub)
            # The loader itself is streamed, not typed.
            pushes = [r for r in link.requests if r[1] == "loader.push"]
            self.assertGreaterEqual(len(pushes), 1)
            pushed = "".join(r[2]["data_b64"] for r in pushes)
            self.assertEqual(
                pushed,
                posix_loader_blob(LINK, manager.status()["session_id"], "/dev/cu.usbmodemTEST"),
            )

    def test_posix_stub_stays_within_the_microloader_budget(self) -> None:
        command = posix_stub_command(LINK, "/dev/ttyACM0")
        self.assertLessEqual(len(command), 1000)
        encoded = command.split('b64decode("', 1)[1].rsplit('"', 1)[0]
        stub = gzip.decompress(base64.b64decode(encoded)).decode("utf-8")
        for forbidden in ("titella-slave.pyz", "sha256", "subprocess", "tempfile"):
            self.assertNotIn(forbidden, stub)

    def test_posix_stub_streams_the_whole_bootstrap_over_a_pty(self) -> None:
        """POSIX mirrors Windows end to end: stub, loader over CDC, payload, agent.

        A PTY stands in for the ESP32 CDC, so the real typed stub, the real
        loader from the wheel and the real payload all run. The test asserts
        every protocol step: stage.ready, the flow-controlled loader stream,
        loader.ready, begin, one ACK per block, commit and the agent's hello.
        """
        import os
        import select
        import tty

        from titella_slave_assets.bundles import load_bundle

        bundle = load_bundle("python")
        master_fd, slave_fd = os.openpty()
        tty.setraw(slave_fd)
        port = os.ttyname(slave_fd)
        command = posix_stub_command(LINK, port).replace(
            "python3 ", sys.executable + " ", 1)
        # A fresh session: the agent the loader detaches stays inside this
        # group, so it can be reaped by the test without touching anything else.
        process = subprocess.Popen(["/bin/sh", "-c", command],
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        session = "0123456789abcdef0123456789abcdef"
        blob = posix_loader_blob(LINK, session, port)
        seen: dict[str, object] = {}
        buffer = b""
        offset = 0
        request_id = 0

        def send(frame: dict) -> None:
            os.write(master_fd, (json.dumps(frame, separators=(",", ":")) + "\n").encode())

        def push_data(index: int) -> None:
            chunk = bundle.payload[index * 512:(index + 1) * 512]
            send({"v": 1, "channel": "bootstrap", "type": "data",
                  "link_id": LINK, "session_id": session,
                  "seq": index, "data_b64": base64.b64encode(chunk).decode()})

        deadline = time.time() + 60
        try:
            while time.time() < deadline and "hello" not in seen:
                if not select.select([master_fd], [], [], 0.2)[0]:
                    continue
                buffer += os.read(master_fd, 65536)
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    frame = json.loads(line)
                    kind, op = frame.get("type"), frame.get("op")
                    if kind == "event" and op == "stage.ready":
                        seen["stage"] = True
                        while offset < len(blob):
                            request_id += 1
                            chunk = blob[offset:offset + 512]
                            send({"v": 1, "channel": "bootstrap", "type": "request",
                                  "link_id": LINK, "request_id": request_id,
                                  "op": "loader.push",
                                  "body": {"offset": offset, "total": len(blob),
                                           "data_b64": chunk}})
                            offset += len(chunk)
                    elif kind == "response" and op == "loader.push":
                        seen["staged"] = frame["body"]["staged"]
                    elif kind == "event" and op == "loader.ready":
                        seen["loader_ready"] = True
                        request_id += 1
                        send({"v": 1, "channel": "bootstrap", "type": "request",
                              "link_id": LINK, "request_id": request_id,
                              "op": "begin",
                              "body": {"session_id": session,
                                       "size": len(bundle.payload),
                                       "sha256": bundle.sha256}})
                    elif kind == "response" and op == "begin":
                        seen["begin"] = True
                        push_data(0)
                        seen["blocks"] = 1
                    elif kind == "ack":
                        index = int(seen.get("blocks", 0))
                        if index * 512 < len(bundle.payload):
                            push_data(index)
                            seen["blocks"] = index + 1
                        else:
                            request_id += 1
                            send({"v": 1, "channel": "bootstrap", "type": "request",
                                  "link_id": LINK, "request_id": request_id,
                                  "op": "commit", "body": {"session_id": session}})
                    elif kind == "response" and op == "commit":
                        seen["commit"] = frame["body"]["status"]
                    elif kind == "hello":
                        seen["hello"] = frame["body"]
        finally:
            os.close(master_fd)
            os.close(slave_fd)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            # The loader spawns the agent detached on purpose, so it survives
            # the stub. Reap whatever still points at this bootstrap directory
            # so a PTY-backed run cannot leave an orphan holding the terminal.
            subprocess.run(
                ["pkill", "-9", "-f", "/.payload.pyz --port " + port],
                check=False,
            )

        self.assertTrue(seen.get("stage"), "the stub never announced stage.ready")
        self.assertEqual(seen.get("staged"), len(blob),
                         "the stub did not accumulate the whole loader")
        self.assertTrue(seen.get("loader_ready"), "the loader never announced loader.ready")
        self.assertTrue(seen.get("begin"), "the loader never accepted begin")
        self.assertEqual(seen.get("commit"), "started", "the loader never committed the payload")
        hello = seen.get("hello") or {}
        self.assertEqual(hello.get("implementation"), "python",
                         "the spawned agent never announced control.hello")

    def test_loader_source_rejects_unvalidated_identifiers(self) -> None:
        for bad in ("A" * 32, "0" * 31, "0" * 33, "0123456789abcdef0123456789abcdeZ"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                loader_source(bad, "b" * 32)

    def test_loader_command_is_ascii_and_fixed_for_supported_layouts(self) -> None:
        command = loader_command(LINK, "a" * 32)
        command.encode("ascii")
        self.assertTrue(command.startswith("python3 -c "))
        self.assertNotIn("curl", command)
        self.assertNotIn("http", command.lower())

        command = loader_command(LINK, "a" * 32)
        command.encode("ascii")
        self.assertTrue(command.startswith("python3 -c "))
        self.assertNotIn("curl", command)
        self.assertNotIn("http", command.lower())


class HidBootstrapTests(unittest.TestCase):
    """The HID leg: preconditions, staging order and cleanup, no CDC."""

    def _config(self, temp: str, **extra: str):
        environ = {
            "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
            "TITELLA_STATE_DIR": temp,
            "TITELLA_SLAVE_PLATFORM": "windows",
            "TITELLA_SLAVE_TRANSPORT": "hid",
        }
        environ.update(extra)
        return load_config(environ=environ)

    def test_hid_requires_an_operator_prepared_console(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            result = manager.bootstrap(
                "operator", "es_ES", platform="windows", layout_confirmed=True,
            )
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "invalid_argument")
            self.assertIn("skip_launch", result["error"]["message"])
            # No lease, no key, no staging: the contradiction is pre-teclas.
            self.assertEqual(hid.lease.owners, [])
            self.assertEqual(hid.keys, [])
            self.assertEqual(hid.typed, [])
            self.assertEqual(link.requests, [])

    def test_hid_rejects_a_cdc_port_and_a_non_windows_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            for call, message in (
                ({"platform": "windows", "cdc_port": "COM5"}, "cdc_port"),
                ({"platform": "macos"}, "platform='windows'"),
            ):
                with self.subTest(call=call):
                    hid = FakeHid()
                    manager = BootstrapManager(config, FakeLink(), hid)
                    result = manager.bootstrap(
                        "operator", "es_ES", layout_confirmed=True,
                        skip_launch=True, **call,
                    )
                    self.assertEqual(result["error"]["code"], "invalid_argument")
                    self.assertIn(message, result["error"]["message"])
                    self.assertEqual(hid.keys, [])
                    self.assertEqual(hid.typed, [])

    def test_old_firmware_is_rejected_before_any_keystroke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            for health_update in (
                {"hid_data_protocol": None},
                {"slave_transports": ["cdc"]},
                {"usb_vid": 1234},
                {"usb_pid": 5678},
                {"usb_serial": "tite-lla"},
            ):
                with self.subTest(health=health_update):
                    link = FakeLink()
                    link.health.update(health_update)
                    hid = FakeHid()
                    manager = BootstrapManager(config, link, hid)
                    result = manager.bootstrap(
                        "operator", "es_ES", platform="windows",
                        layout_confirmed=True, skip_launch=True,
                    )
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error"]["code"],
                                     "slave_transport_unsupported")
                    self.assertEqual(hid.keys, [])
                    self.assertEqual(hid.typed, [])
                    # Only the fresh health query reached the firmware.
                    self.assertEqual(
                        [r[1] for r in link.requests], ["health"],
                    )

    def test_unreachable_health_is_reported_as_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()

            def refuse(channel, op, body, timeout=10):
                if channel == "hid" and op == "health":
                    raise TransportError("request_timeout", "no hid.health", True)
                return FakeLink.request(link, channel, op, body, timeout)

            link.request = refuse  # type: ignore[assignment]
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            result = manager.bootstrap(
                "operator", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
            self.assertEqual(result["error"]["code"], "slave_transport_unsupported")
            self.assertEqual(hid.typed, [])

    def test_hid_stages_the_snapshot_before_typing_and_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            original_type = hid.type_text
            stage_seen_at_typing: list[bool] = []

            def type_text(owner, text, delay):
                if not hid.typed:
                    stage_seen_at_typing.append(
                        any(op == "loader_stage_commit" for _c, op, _b in link.requests)
                    )
                return original_type(owner, text, delay)
            hid.type_text = type_text  # type: ignore[assignment]

            def ready_after_enter():
                link.emit({
                    "v": 1, "channel": "bootstrap", "type": "event", "link_id": LINK,
                    "session_id": manager.status()["session_id"],
                    "op": "loader.ready", "body": {"runtime": "stage0"},
                })
            hid.on_enter = ready_after_enter

            result = manager.bootstrap(
                "operator", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["slave_transport"], "hid")
            self.assertEqual(result["hid_binding"], "HID:012345ABCDEF")
            # The snapshot is committed before any typed character.
            self.assertEqual(stage_seen_at_typing, [True])
            ops = [op for _c, op, _b in link.requests]
            self.assertIn("loader_stage_begin", ops)
            self.assertIn("loader_stage_commit", ops)
            self.assertEqual(ops[-1], "loader_stage_clear")
            # The staged bytes are the gzip of the HID container (rendered
            # loader, marker, transport unit), not a second render: their
            # SHA-256 is the one handed to begin/commit.
            begin = next(b for _c, op, b in link.requests if op == "loader_stage_begin")
            import hashlib
            self.assertEqual(hashlib.sha256(bytes(link.staged_bytes)).hexdigest(),
                             begin["sha256"])
            self.assertEqual(len(link.staged_bytes), begin["size"])
            # No CDC path ran: no stub typing, no loader.push, no clipboard.
            self.assertFalse(any("stage.ready" in chunk for chunk in hid.typed))
            self.assertFalse(any("Get-Clipboard" in chunk for chunk in hid.typed))
            self.assertNotIn("loader.push", ops)
            # The destination is selected before the layout is touched.
            self.assertEqual(
                ops.index("slave_transport") < ops.index("loader_stage_begin"), True)

    def test_hid_snapshot_container_carries_the_unit_behind_one_marker(self) -> None:
        """The staged blob is the #@# container: the transport unit travels
        as raw UTF-8 after exactly one marker and never as Base64 script
        text inside the rendered loader."""
        import gzip
        from titella.bootstrap import _hid_container

        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)

            def ready_after_enter():
                link.emit({
                    "v": 1, "channel": "bootstrap", "type": "event", "link_id": LINK,
                    "session_id": manager.status()["session_id"],
                    "op": "loader.ready", "body": {"runtime": "stage0"},
                })
            hid.on_enter = ready_after_enter

            result = manager.bootstrap(
                "operator", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
            self.assertEqual(result["status"], "ready", result)
            container = gzip.decompress(bytes(link.staged_bytes)).decode("utf-8")
            bundle = load_bundle("windows-dotnet")
            self.assertEqual(container.count("#@#"), 1,
                             "the marker must appear exactly once")
            loader_part, _, unit_part = container.partition("#@#")
            self.assertEqual(unit_part.strip(), bundle.hid_transport_source.strip())
            self.assertNotIn(bundle.hid_transport_source, loader_part)
            self.assertNotIn(
                base64.b64encode(bundle.hid_transport_source.encode("utf-8")).decode("ascii"),
                loader_part,
                "the unit must not travel embedded as Base64",
            )
            # The marker may appear in neither half: the packer rejects it.
            with self.assertRaisesRegex(ValueError, "container marker"):
                _hid_container("x#@#y", bundle.hid_transport_source)
            with self.assertRaisesRegex(ValueError, "container marker"):
                _hid_container(loader_part, "a#@#b")

    def test_hid_clears_the_slot_even_when_the_run_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            link.stage_failures["loader_stage_commit"] = "checksum_mismatch"
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            result = manager.bootstrap(
                "operator", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "checksum_mismatch")
            ops = [op for _c, op, _b in link.requests]
            self.assertEqual(ops[-1], "loader_stage_clear")
            self.assertEqual(hid.typed, [])
            status = manager.status()
            self.assertEqual(status["status"], "error")
            self.assertEqual(status["slave_transport"], "hid")

    def test_hid_oversized_loader_fails_before_publishing_the_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            import titella.bootstrap as bootstrap_module
            saved = bootstrap_module._HID_SNAPSHOT_MAX_BYTES
            try:
                bootstrap_module._HID_SNAPSHOT_MAX_BYTES = 16
                result = manager.bootstrap(
                    "operator", "es_ES", platform="windows",
                    layout_confirmed=True, skip_launch=True,
                )
            finally:
                bootstrap_module._HID_SNAPSHOT_MAX_BYTES = saved
            self.assertEqual(result["error"]["code"], "hid_loader_too_large")
            ops = [op for _c, op, _b in link.requests]
            self.assertNotIn("loader_stage_begin", ops)
            self.assertEqual(hid.typed, [])

    def test_hid_oversized_stage0_fails_before_typing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            import titella.bootstrap as bootstrap_module
            saved = bootstrap_module._HID_STAGE0_MAX_CHARS
            try:
                bootstrap_module._HID_STAGE0_MAX_CHARS = 64
                result = manager.bootstrap(
                    "operator", "es_ES", platform="windows",
                    layout_confirmed=True, skip_launch=True,
                )
            finally:
                bootstrap_module._HID_STAGE0_MAX_CHARS = saved
            self.assertEqual(result["error"]["code"], "hid_stage0_too_large")
            self.assertEqual(hid.typed, [])
            ops = [op for _c, op, _b in link.requests]
            self.assertNotIn("loader_stage_begin", ops)


    def test_stop_does_not_report_finished_while_the_stream_stays_attached(self) -> None:
        """A stop reply (or its timeout) is never proof of a closed stream."""
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            for stop_reply in (None, "request_timeout", "route_error"):
                with self.subTest(stop_reply=stop_reply):
                    link = FakeLink()
                    hid = FakeHid()
                    manager = BootstrapManager(config, link, hid)
                    link.selected_transport = "hid"
                    link.slave_connected = True
                    link.stop_reply = stop_reply
                    if stop_reply is None:
                        # The agent answers but never releases the stream.
                        link.stop_releases = False
                    # The agent never releases the stream: no reply may be
                    # reported and the gate must stay closed.
                    with self.assertRaises(TransportError) as ctx:
                        manager.stop()
                    self.assertEqual(ctx.exception.code, "agent_stop_timeout")
                    self.assertFalse(manager.gate.ready)
                    self.assertNotEqual(manager.status().get("status"), "stopped")

    def test_stop_returns_once_the_firmware_reports_the_stream_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            link.selected_transport = "hid"
            link.slave_connected = True
            # The stop timed out, but the close is observable right away: the
            # report is success because the state, not the RPC, says closed.
            link.stop_reply = "request_timeout"
            link.slave_connected = False
            result = manager.stop()
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(manager.status()["status"], "stopped")
            self.assertFalse(manager.gate.ready)

    def test_bootstrap_does_not_select_or_type_while_the_stream_is_attached(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            link.selected_transport = "hid"
            link.slave_connected = True  # a previous agent still owns the epoch
            link.stop_reply = "request_timeout"
            result = manager.bootstrap(
                "operator", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "agent_stop_timeout")
            ops = [op for _c, op, _b in link.requests]
            # No destination change, no staging and no keystroke happened.
            self.assertNotIn("slave_transport", ops)
            self.assertNotIn("loader_stage_begin", ops)
            self.assertEqual(hid.keys, [])
            self.assertEqual(hid.typed, [])
            self.assertEqual(hid.layouts, [])

    def test_bootstrap_waits_until_the_old_stream_is_observably_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            link.selected_transport = "hid"
            link.slave_connected = True
            link.stop_reply = "request_timeout"
            probes = {"count": 0}
            original_request = link.request

            def delayed_close(channel, op, body, timeout=10):
                if channel == "hid" and op == "health":
                    probes["count"] += 1
                    if probes["count"] >= 3:
                        link.slave_connected = False
                return original_request(channel, op, body, timeout)

            link.request = delayed_close  # type: ignore[assignment]
            hid.on_enter = lambda: link.emit({
                "v": 1, "channel": "bootstrap", "type": "event", "link_id": LINK,
                "session_id": manager.status()["session_id"],
                "op": "loader.ready", "body": {"runtime": "stage0"},
            })
            result = manager.bootstrap(
                "operator", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
            self.assertEqual(result["status"], "ready", result)
            self.assertGreaterEqual(probes["count"], 3)
            ops = [op for _c, op, _b in link.requests]
            # The destination was selected only after the close was observed.
            self.assertLess(ops.index("health"), ops.index("slave_transport"))

    def test_stop_wait_proves_close_on_either_selected_destination(self) -> None:
        """A HID instance may have to stop an agent that opened the CDC leg."""
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            for selected, connected in (("cdc", True), ("cdc", False),
                                        ("hid", True), ("hid", False)):
                with self.subTest(selected=selected, connected=connected):
                    link = FakeLink()
                    hid = FakeHid()
                    manager = BootstrapManager(config, link, hid)
                    link.selected_transport = selected
                    link.slave_connected = connected
                    link.stop_reply = "request_timeout"
                    if connected:
                        with self.assertRaises(TransportError) as ctx:
                            manager.stop()
                        self.assertEqual(ctx.exception.code, "agent_stop_timeout")
                        self.assertNotEqual(manager.status().get("status"), "stopped")
                    else:
                        self.assertEqual(manager.stop()["status"], "stopped")
                        self.assertEqual(manager.status()["status"], "stopped")

    def test_hid_bootstrap_waits_for_a_cdc_open_then_allows_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            # A previous agent is on the CDC leg, which the firmware still
            # forwards to: the stop timed out and the line stays open.
            link.selected_transport = "cdc"
            link.slave_connected = True
            link.stop_reply = "request_timeout"
            result = manager.bootstrap(
                "operator", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
            self.assertEqual(result["error"]["code"], "agent_stop_timeout")
            ops = [op for _c, op, _b in link.requests]
            self.assertNotIn("slave_transport", ops)
            self.assertEqual(hid.keys, [])
            self.assertEqual(hid.typed, [])

            # The host line closes: the same flow proceeds, and only then
            # switches the firmware's destination.
            probes = {"count": 0}
            original_request = link.request

            def closes_after_two_probes(channel, op, body, timeout=10):
                if channel == "hid" and op == "health":
                    probes["count"] += 1
                    if probes["count"] >= 2:
                        link.slave_connected = False
                return original_request(channel, op, body, timeout)

            link.request = closes_after_two_probes  # type: ignore[assignment]
            link.requests.clear()
            hid.on_enter = lambda: link.emit({
                "v": 1, "channel": "bootstrap", "type": "event", "link_id": LINK,
                "session_id": manager.status()["session_id"],
                "op": "loader.ready", "body": {"runtime": "stage0"},
            })
            result = manager.bootstrap(
                "operator", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
            self.assertEqual(result["status"], "ready", result)
            ops = [op for _c, op, _b in link.requests]
            self.assertIn("slave_transport", ops)
            self.assertLess(ops.index("health"), ops.index("slave_transport"))

    def test_hid_config_cannot_claim_stopped_without_the_state_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            original_request = link.request

            def fieldless_health(channel, op, body, timeout=10):
                if channel == "hid" and op == "health":
                    frame = original_request(channel, op, body, timeout)
                    frame["body"].pop("slave_connected", None)
                    return frame
                return original_request(channel, op, body, timeout)

            link.request = fieldless_health  # type: ignore[assignment]
            link.selected_transport = "hid"
            link.slave_connected = True
            with self.assertRaises(TransportError) as ctx:
                manager.stop()
            self.assertEqual(ctx.exception.code, "agent_stop_timeout")
            self.assertNotEqual(manager.status().get("status"), "stopped")

    def test_stop_wait_is_skipped_where_the_state_is_not_observable(self) -> None:
        """A CDC-configured instance keeps its previous behaviour."""
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
                "TITELLA_SLAVE_PLATFORM": "windows",
            })
            self.assertEqual(config.slave_transport, "cdc")
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            original_request = link.request

            def legacy_health(channel, op, body, timeout=10):
                if channel == "hid" and op == "health":
                    frame = original_request(channel, op, body, timeout)
                    frame["body"].pop("slave_connected", None)
                    return frame
                return original_request(channel, op, body, timeout)

            link.request = legacy_health  # type: ignore[assignment]
            link.selected_transport = "cdc"
            link.slave_connected = True
            self.assertEqual(manager.stop()["status"], "stopped")


    def test_failure_reports_the_attempt_binding_and_never_a_stale_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            # A previous attempt left a binding in the merged status.
            manager._set(status="error", hid_binding="HID:FFFFFFFFFFFF")
            link.stage_failures["loader_stage_commit"] = "checksum_mismatch"
            result = manager.bootstrap(
                "operator", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["slave_transport"], "hid")
            self.assertEqual(result["hid_binding"], "HID:012345ABCDEF")
            status = manager.status()
            self.assertEqual(status["slave_transport"], "hid")
            self.assertEqual(status["hid_binding"], "HID:012345ABCDEF")

    def test_unsupported_health_clears_the_binding_and_names_the_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            link.health.update({"hid_data_protocol": None})
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            manager._set(status="error", hid_binding="HID:FFFFFFFFFFFF")
            result = manager.bootstrap(
                "operator", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
            self.assertEqual(result["error"]["code"], "slave_transport_unsupported")
            self.assertEqual(result["slave_transport"], "hid")
            self.assertNotIn("hid_binding", result)
            status = manager.status()
            self.assertEqual(status["slave_transport"], "hid")
            self.assertIsNone(status["hid_binding"])

    def test_early_argument_rejection_does_not_touch_the_live_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(temp)
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            manager._set(status="ready", slave_transport="hid",
                         hid_binding="HID:012345ABCDEF", stage_elapsed_s=1.5)
            result = manager.bootstrap(
                "operator", "es_ES", platform="windows", layout_confirmed=True,
            )  # skip_launch missing: rejected before any keystroke
            self.assertEqual(result["error"]["code"], "invalid_argument")
            status = manager.status()
            self.assertEqual(status["status"], "ready")
            self.assertEqual(status["hid_binding"], "HID:012345ABCDEF")
            self.assertEqual(status["stage_elapsed_s"], 1.5)

    def test_cdc_ready_result_clears_a_leftover_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(environ={
                "TITELLA_CONFIG": str(Path(temp) / "absent.toml"),
                "TITELLA_STATE_DIR": temp,
                "TITELLA_SLAVE_PLATFORM": "linux",
                "TITELLA_TERMINAL_MODS": "SUPER",
                "TITELLA_TERMINAL_KEY": "RETURN",
            })
            link = FakeLink()
            hid = FakeHid()
            manager = BootstrapManager(config, link, hid)
            manager._set(hid_binding="HID:FFFFFFFFFFFF")
            hid.on_enter = lambda: link.emit({
                "v": 1, "channel": "bootstrap", "type": "event", "link_id": LINK,
                "session_id": manager.status()["session_id"],
                "op": "loader.ready", "body": {"runtime": "python"},
            })
            result = manager.bootstrap(
                "operator", "en_US", platform="linux", layout_confirmed=True,
            )
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["slave_transport"], "cdc")
            self.assertIsNone(result["hid_binding"])
            self.assertIsNone(manager.status()["hid_binding"])


if __name__ == "__main__":
    unittest.main()
