from __future__ import annotations

import base64
import gzip
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from titella.bootstrap import (
    BootstrapManager,
    loader_command,
    loader_source,
    posix_loader_blob,
    posix_stub_command,
)
from titella.config import load_config
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
        if channel == "bootstrap" and op == "loader.push":
            self.staged = getattr(self, "staged", 0) + len(body["data_b64"])
            return {"body": {"status": "staged", "staged": self.staged,
                             "total": body["total"]}}
        if channel == "bootstrap" and op == "commit":
            self.emit({
                "v": 1, "channel": "control", "type": "hello", "link_id": LINK,
                "body": {"uid": 501, "python": "3.9.18"},
            })
            return {"body": {"status": "started"}}
        if channel == "control" and op == "bind":
            return {"body": {"status": "ready", "link_id": LINK}}
        if channel == "control" and op == "agent.stop":
            return {"body": {"status": "stopped"}}
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

    def test_windows_profile_uses_start_search_powershell_and_the_cs_payload(self) -> None:
        from titella.bootstrap import PLATFORM_PROFILES, windows_loader_lines
        profile = PLATFORM_PROFILES["windows"]
        self.assertEqual((profile.loader_wait_s, profile.hello_wait_s), (30, 90))
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
            result = manager.bootstrap("operator", "en_US", layout_confirmed=True)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["platform"], "windows")
            self.assertEqual(result["implementation"], "windows-dotnet")
            self.assertEqual(hid.keys[0], ("operator", "ESC", ("CTRL",)))
            self.assertEqual(hid.typed[0], "powershell")
            self.assertEqual(hid.keys[2:4], [
                ("operator", "ESC", ()),
                ("operator", "C", ("CTRL",)),
            ])

            # The loader is pushed over the CDC and only the stub is typed.
            pushes = [r for r in link.requests if r[1] == "loader.push"]
            self.assertGreaterEqual(len(pushes), 1)
            pushed = "".join(r[2]["data_b64"] for r in pushes)
            from titella.bootstrap import windows_loader_blob
            self.assertEqual(pushed, windows_loader_blob(
                LINK, manager.status()["session_id"], "COM5"))
            self.assertEqual(pushes[-1][2]["total"], len(pushed))

            # Rebuild the console lines from the ordered transcript: each line
            # is submitted with its own Enter.
            lines: list[str] = []
            current: list[str] = []
            for kind, *payload in hid.events:
                if kind == "type":
                    current.append(payload[0])
                elif payload[0] == "ENTER":
                    lines.append("".join(current))
                    current = []
            self.assertEqual(lines[0], "powershell")
            from titella.bootstrap import windows_stub_command
            self.assertEqual(lines[1], windows_stub_command(LINK, "COM5"))
            self.assertEqual(len(lines), 2)

            # The typed stub avoids the backtick dead key (`es_ES` maps `` ` ``
            # to 0x00 in Arduino USBHIDKeyboard), prints per-chunk progress and
            # disables PSReadLine history saving before anything else runs.
            self.assertNotIn("`", lines[1])
            self.assertIn("[Console]::Write(++$n)", lines[1])
            self.assertIn("$p.WriteLine(", lines[1])
            self.assertIn(
                "Set-PSReadLineOption -HistorySaveStyle SaveNothing", lines[1])

            # The typed text is only the flow-controlled stub (~796 chars), not
            # the ~6 500-char loader.
            typed = sum(len(text) for text in hid.typed)
            self.assertLess(typed, 800)
            # Every HID call stays bounded and Windows is typed at 8 ms/char.
            self.assertTrue(all(len(text) <= 256 for text in hid.typed))
            self.assertTrue(all(delay == 8 for delay in hid.typed_delays))
            status = manager.status()
            self.assertEqual(status["stage_chars_sent"], len(pushed))
            self.assertEqual(status["stage_chars_total"], len(pushed))
            self.assertEqual(status["stage_chunks_acked"], len(pushes))
            self.assertEqual(status["stage_chunks_total"], len(pushes))

            # The pushed blob reconstructs the wheel's readable Windows loader.
            rendered = gzip.decompress(base64.b64decode(pushed)).decode("utf-8")
            self.assertEqual(rendered, loader_source(
                LINK, manager.status()["session_id"], "COM5",
                implementation="windows-dotnet",
            ))
            for forbidden in ("ExecutionPolicy Bypass", "Unblock-File",
                              "-WindowStyle Hidden", "-File "):
                self.assertNotIn(forbidden, rendered)
            # The transferred payload is exactly the selected bundle.
            bundle = load_bundle("windows-dotnet")
            self.assertEqual(result["size"], len(bundle.payload))
            self.assertEqual(bytes(link.payload), bundle.payload)
            self.assertEqual(result["sha256"], bundle.sha256)

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
                ["pkill", "-9", "-f", tempfile.gettempdir() + "/titella-bootstrap-"],
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


if __name__ == "__main__":
    unittest.main()
