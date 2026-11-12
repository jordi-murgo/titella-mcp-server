"""Window transport: configuration, link surface, capture and diagnostics.

The tests never touch a real desktop window: ``WindowLink`` accepts providers
for window discovery, capture and HID injection, so every assertion runs
against a recorded call list.
"""
from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from titella.agent_state import require_cdc_channel
from titella.bootstrap import (
    CLIPBOARD_STAGE1,
    BootstrapManager,
    windows_clipboard_stub_command,
)
from titella.config import load_config, save_titella_config
from titella.diagnostics import diagnose, next_steps
from titella.firmware_info import FirmwareInfo
from titella.hid import HidController, HidLease
from titella.png_codec import encode_png_rgb
from titella.registry import TitellaInstance, TitellaRegistry
from titella.screen import ScreenCapture
from titella.serial_link import TransportError
from titella.window_link import (
    FLAG_COMMAND,
    FLAG_CONTROL,
    FLAG_SHIFT,
    WindowLink,
    decode_steps,
    macos_key_event,
)

WINDOW = {
    "id": 4242,
    "pid": 707,
    "owner": "Citrix Viewer",
    "title": "remote session",
    "bounds": {"x": 100, "y": 50, "width": 800, "height": 600},
}


def _state_dir() -> str:
    return tempfile.mkdtemp(prefix="titella-window-test-")


def window_config(**overrides):
    """A window-transport config; overrides win over the defaults."""
    environment = {
        "TITELLA_STATE_DIR": _state_dir(),
        "TITELLA_TRANSPORT": "window",
        "TITELLA_WINDOW_APP": "Citrix Viewer",
    }
    environment.update(overrides)
    return load_config(environ=environment)


def border_png(width: int, height: int, border: int, fill: int = 0x90) -> bytes:
    """A PNG with a black border of ``border`` pixels on every side."""
    rgb = bytearray(width * height * 3)
    for y in range(border, height - border):
        for x in range(border, width - border):
            offset = (y * width + x) * 3
            rgb[offset:offset + 3] = bytes((fill, fill, fill))
    return encode_png_rgb(width, height, bytes(rgb))


class RecordingBackend:
    """Records helper argv and answers ok, like the compiled helper would."""

    def __init__(self, reply: dict | None = None) -> None:
        self.calls: list[list[str]] = []
        self.reply = reply or {"ok": True}

    def __call__(self, argv: list[str]) -> dict:
        self.calls.append(list(argv))
        return dict(self.reply)

    def subcommands(self) -> list[str]:
        return [call[0] for call in self.calls]

    def injections(self) -> list[str]:
        """Subcommands that carry an event, without the wake/sleep bookkeeping."""
        return [call[0] for call in self.calls if call[0] not in {"wake", "sleep"}]

    def last(self) -> list[str]:
        return self.calls[-1]


class FakeWindowLink:
    """A minimal window link for ScreenCapture-only tests."""

    transport = "window"

    def __init__(self, window: dict | None, png: bytes | None, failure: dict | None = None) -> None:
        self.window = window
        self.png = png
        self.failure = failure
        self.port = f"window:{window['id']}" if window else None

    def capture_window_png(self, output_path: Path):
        if self.failure is not None:
            return None, self.failure
        output_path.write_bytes(self.png or b"")
        return dict(self.window or {}), None


class WindowConfigTests(unittest.TestCase):
    def test_window_transport_is_accepted_and_selectors_load(self) -> None:
        config = window_config(TITELLA_WINDOW_TITLE="remote")
        self.assertEqual(config.transport, "window")
        self.assertEqual(config.window_app, "Citrix Viewer")
        self.assertEqual(config.window_title, "remote")
        self.assertIsNone(config.window_id)

    def test_window_id_env_override_is_parsed_as_integer(self) -> None:
        config = window_config(TITELLA_WINDOW_ID="12345")
        self.assertEqual(config.window_id, 12345)

    def test_window_transport_requires_a_selector(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "window_app, window_title or window_id is required with transport=window"
        ):
            load_config(
                environ={"TITELLA_STATE_DIR": _state_dir(), "TITELLA_TRANSPORT": "window"}
            )

    def test_window_id_is_validated_even_off_the_window_transport(self) -> None:
        for value in ("0", "-1", "2147483648", "not-an-id"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "window_id"):
                load_config(environ={"TITELLA_STATE_DIR": _state_dir(), "TITELLA_WINDOW_ID": value})

    def test_transport_error_names_all_three_transports(self) -> None:
        with self.assertRaisesRegex(ValueError, "transport must be serial, wifi or window"):
            load_config(environ={"TITELLA_STATE_DIR": _state_dir(), "TITELLA_TRANSPORT": "tcp"})

    def test_window_toml_round_trip_keeps_the_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            save_titella_config(
                path,
                global_values={"state_dir": str(root / "state"), "http_port": 8765},
                titellas={
                    "desk": {
                        "transport": "window",
                        "window_app": "Citrix Viewer",
                        "window_title": "remote session",
                        "window_id": 4242,
                    }
                },
                default_titella="desk",
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn('window_app = "Citrix Viewer"', text)
            self.assertIn('window_title = "remote session"', text)
            self.assertIn("window_id = 4242", text)
            reloaded = load_config(path, {})
            desk = reloaded.titellas["desk"]
            self.assertEqual(desk.transport, "window")
            self.assertEqual(desk.window_app, "Citrix Viewer")
            self.assertEqual(desk.window_title, "remote session")
            self.assertEqual(desk.window_id, 4242)

    def test_serial_and_wifi_configuration_is_untouched(self) -> None:
        serial = load_config(environ={"TITELLA_STATE_DIR": _state_dir()})
        self.assertEqual(serial.transport, "serial")
        self.assertIsNone(serial.window_app)
        wifi = load_config(
            environ={"TITELLA_STATE_DIR": _state_dir(), "TITELLA_TRANSPORT": "wifi"}
        )
        self.assertEqual(wifi.transport, "wifi")
        self.assertEqual(wifi.wifi_port, 5555)


class WindowLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = window_config()
        self.backend = RecordingBackend()
        self.window_available = True
        self.link = WindowLink(
            self.config,
            window_provider=self._provider,
            helper_provider=self.backend,
        )

    def _provider(self) -> dict:
        if not self.window_available:
            return {"ok": False, "code": "window_not_found", "message": "no window matches"}
        return {"ok": True, "window": dict(WINDOW)}

    def test_find_window_reports_identity_and_persists_the_link_id(self) -> None:
        found = self.link.find_window()
        self.assertTrue(found["ok"])
        self.assertEqual(found["window"]["id"], 4242)
        self.assertTrue(self.link.connected)
        self.assertEqual(self.link.state, "connected")
        self.assertEqual(self.link.port, "window:4242")
        self.assertEqual(len(self.link.link_id), 32)
        stored = (self.link.state_dir / "active-link-id").read_text(encoding="ascii")
        self.assertEqual(stored, self.link.link_id)

    def test_missing_window_disconnects_and_raises_retryable(self) -> None:
        self.window_available = False
        self.assertFalse(self.link.find_window()["ok"])
        self.assertFalse(self.link.connected)
        self.assertEqual(self.link.state, "disconnected")
        with self.assertRaises(TransportError) as ctx:
            self.link.request("hid", "kb_key", {"key": "ENTER"})
        self.assertEqual(ctx.exception.code, "window_not_found")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(self.backend.calls, [])

    def test_non_hid_channels_answer_cdc_unavailable(self) -> None:
        with self.assertRaises(TransportError) as ctx:
            self.link.request("shell", "job.start", {"cmd": "dir"})
        self.assertEqual(ctx.exception.code, "cdc_unavailable")
        self.assertFalse(ctx.exception.retryable)

    def test_health_announces_the_window_and_a_protocol(self) -> None:
        body = self.link.request("hid", "health", {})["body"]
        self.assertTrue(body["ok"])
        self.assertEqual(body["firmware"], "window-1.0")
        self.assertEqual(body["protocol"], 1)
        self.assertEqual(body["window"]["id"], 4242)

    def test_unknown_hid_op_is_reported(self) -> None:
        with self.assertRaises(TransportError) as ctx:
            self.link.request("hid", "agent_ping", {})
        self.assertEqual(ctx.exception.code, "unsupported_hid_op")

    def test_subscription_stubs_are_no_ops(self) -> None:
        self.assertEqual(self.link.subscribe("hid", lambda frame: True)(), None)
        self.assertEqual(self.link.subscribe_state(lambda state: None)(), None)
        self.link.start()
        self.link.close(clean=True)
        self.assertTrue(self.link.wait_connected(0))

    def test_kb_layout_validates_and_tracks_the_selection(self) -> None:
        self.assertEqual(self.link.request("hid", "kb_layout", {"layout": "en_US"})["body"]["ok"], True)
        with self.assertRaisesRegex(ValueError, "es_ES or en_US"):
            self.link.request("hid", "kb_layout", {"layout": "fr_FR"})
        self.assertEqual(self.backend.calls, [])

    def test_kb_text_types_exact_unicode(self) -> None:
        result = self.link.request(
            "hid", "kb_text", {"text": "ñ€á", "delay": 3, "enter": True}
        )["body"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["characters"], 3)
        call = self.backend.last()
        self.assertEqual(call[0], "unicode")
        self.assertEqual(call[call.index("--pid") + 1], "707")
        encoded = call[call.index("--text-b64") + 1]
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), "ñ€á")
        self.assertEqual(call[call.index("--enter") + 1], "1")

    def test_kb_raw_prefers_the_literal_text_when_present(self) -> None:
        self.link.request(
            "hid", "kb_raw",
            {"steps": ["00:34", "00:04"], "text": "á", "delay": 0, "enter": True},
        )
        call = self.backend.last()
        self.assertEqual(call[0], "unicode")
        self.assertEqual(
            base64.b64decode(call[call.index("--text-b64") + 1]).decode("utf-8"), "á"
        )

    def test_kb_raw_decodes_steps_without_text(self) -> None:
        self.link.request("hid", "kb_raw", {"steps": ["00:04", "02:04"], "delay": 0})
        self.assertEqual(self.backend.injections(), ["key", "key"])
        first, second = [c for c in self.backend.calls if c[0] == "key"]
        self.assertEqual(first[first.index("--keycode") + 1], "0")       # a
        self.assertEqual(first[first.index("--flags") + 1], "0")
        self.assertEqual(second[second.index("--keycode") + 1], "0")     # shift+a
        self.assertEqual(second[second.index("--flags") + 1], str(FLAG_SHIFT))

    def test_kb_raw_rejects_an_unmapped_usage(self) -> None:
        with self.assertRaises(TransportError) as ctx:
            self.link.request("hid", "kb_raw", {"steps": ["00:ff"], "delay": 0})
        self.assertEqual(ctx.exception.code, "unsupported_hid_step")

    def test_kb_key_maps_names_characters_and_modifiers(self) -> None:
        self.link.request("hid", "kb_key", {"key": "ENTER", "mods": []})
        self.assertEqual(self.backend.last()[self.backend.last().index("--keycode") + 1], "36")
        self.link.request("hid", "kb_key", {"key": "c", "mods": ["CTRL"]})
        call = self.backend.last()
        self.assertEqual(call[call.index("--keycode") + 1], "8")
        self.assertEqual(call[call.index("--flags") + 1], str(FLAG_CONTROL))
        with self.assertRaisesRegex(ValueError, "unknown key"):
            self.link.request("hid", "kb_key", {"key": "NOPE", "mods": []})
        with self.assertRaisesRegex(ValueError, "unknown modifier"):
            self.link.request("hid", "kb_key", {"key": "a", "mods": ["HYPER"]})

    def test_release_all_owns_no_lease_and_releases_held_buttons(self) -> None:
        self.link.request("hid", "mouse_click", {"button": "left", "state": "down"})
        before = len(self.backend.calls)
        result = self.link.request("hid", "release_all", {})["body"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["released"], ["left"])
        self.assertEqual(self.link.request("hid", "release_all", {})["body"]["released"], [])
        releases = [
            call for call in self.backend.calls[before:]
            if call[0] == "mouse" and call[call.index("--action") + 1] == "up"
        ]
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0][releases[0].index("--button") + 1], "left")

    def test_mouse_abs_without_a_capture_spans_the_window(self) -> None:
        body = self.link.request("hid", "mouse_abs", {"x": 0, "y": 32767})["body"]
        # Bounds are x=100..899 and y=50..649, so the extremes land on the edges.
        self.assertEqual(body["screen"], [100, 649])

    def test_mouse_rel_clamps_inside_the_window_and_tracks_the_pointer(self) -> None:
        self.link.request("hid", "mouse_abs", {"x": 32767, "y": 32767})
        moved = self.link.request("hid", "mouse_rel", {"x": 50, "y": 50})["body"]
        self.assertEqual(moved["screen"], [899, 649])
        back = self.link.request("hid", "mouse_rel", {"x": -20, "y": -30})["body"]
        self.assertEqual(back["screen"], [879, 619])

    def test_mouse_abs_rejects_out_of_range_hid_coordinates(self) -> None:
        for x, y in ((32768, 0), (0, -1)):
            with self.subTest(x=x, y=y), self.assertRaisesRegex(ValueError, "0..32767"):
                self.link.request("hid", "mouse_abs", {"x": x, "y": y})

    def test_click_down_then_drag_then_up_composes_the_drag(self) -> None:
        self.link.request("hid", "mouse_abs", {"x": 16383, "y": 16383})
        down = self.link.request("hid", "mouse_click", {"button": "left", "state": "down"})["body"]
        self.assertEqual(down["held"], 1)
        drag = self.link.request(
            "hid", "mouse_abs", {"x": 20000, "y": 20000, "button": "left", "state": "keep"}
        )["body"]
        self.assertEqual(drag["action"], "drag")
        self.assertEqual(self.backend.last()[self.backend.last().index("--held") + 1], "1")
        up = self.link.request("hid", "mouse_click", {"button": "left", "state": "up"})["body"]
        self.assertEqual(up["held"], 0)

    def test_double_click_carries_the_flag(self) -> None:
        self.link.request("hid", "mouse_click", {"button": "left", "double": True})
        call = self.backend.last()
        self.assertEqual(call[call.index("--double") + 1], "1")

    def test_wheel_posts_its_delta_at_the_tracked_position(self) -> None:
        self.link.request("hid", "mouse_abs", {"x": 16383, "y": 16383})
        body = self.link.request("hid", "mouse_wheel", {"delta": 3})["body"]
        self.assertTrue(body["ok"])
        call = self.backend.last()
        self.assertEqual(call[0], "mouse")
        self.assertEqual(call[call.index("--delta") + 1], "3")


class KeyMappingTests(unittest.TestCase):
    def test_named_keys_modifiers_and_shifted_characters(self) -> None:
        self.assertEqual(macos_key_event("ENTER"), (36, 0))
        self.assertEqual(macos_key_event("ESC"), (53, 0))
        self.assertEqual(macos_key_event("F5"), (96, 0))
        self.assertEqual(macos_key_event("A"), (0, FLAG_SHIFT))
        self.assertEqual(macos_key_event("?", ()), (44, FLAG_SHIFT))
        self.assertEqual(macos_key_event("c", ["cmd"]), (8, FLAG_COMMAND))
        self.assertEqual(
            macos_key_event("TAB", ["CTRL", "SHIFT"]), (48, FLAG_CONTROL | FLAG_SHIFT)
        )

    def test_steps_decode_modifier_and_usage(self) -> None:
        self.assertEqual(decode_steps(["00:04", "42:18"]), [(0x00, 0x04), (0x42, 0x18)])
        with self.assertRaisesRegex(ValueError, "invalid HID step"):
            decode_steps(["nonsense"])


class WindowCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = replace(window_config(), screen_autocrop=True)

    def test_captured_window_is_cropped_and_measured(self) -> None:
        link = FakeWindowLink(dict(WINDOW), border_png(200, 160, 20))
        capture = ScreenCapture(self.config, window_link=link)
        metadata, image = capture.capture()
        self.assertTrue(metadata["ok"], metadata)
        self.assertIsNotNone(image)
        self.assertEqual(metadata["source"], "window:4242")
        self.assertEqual(metadata["capture_resolution"], {"width_px": 160, "height_px": 120})
        self.assertEqual(metadata["raw_resolution"], {"width_px": 200, "height_px": 160})
        self.assertEqual(metadata["crop_box"], {"x": 20, "y": 20, "width": 160, "height": 120})
        self.assertEqual(capture.resolution(), (160, 120))
        state = json.loads(capture.resolution_path.read_text(encoding="utf-8"))
        self.assertEqual(state["w"], 160)
        self.assertEqual(state["raw_w"], 200)
        self.assertEqual(state["crop_box"]["x"], 20)

    def test_missing_window_becomes_a_retryable_capture_failure(self) -> None:
        link = FakeWindowLink(
            None,
            None,
            failure={
                "ok": False,
                "status": "error",
                "error": {"code": "window_not_found", "message": "no window", "retryable": True},
            },
        )
        capture = ScreenCapture(self.config, window_link=link)
        metadata, image = capture.capture()
        self.assertIsNone(image)
        self.assertFalse(metadata["ok"])
        self.assertEqual(metadata["error"]["code"], "window_not_found")
        self.assertTrue(metadata["error"]["retryable"])

    def test_a_window_diff_reports_the_changed_region(self) -> None:
        first = border_png(200, 160, 20)
        second_rgb = bytearray(200 * 160 * 3)
        for y in range(20, 140):
            for x in range(20, 180):
                offset = (y * 200 + x) * 3
                second_rgb[offset:offset + 3] = b"\x90\x90\x90"
        for y in range(40, 50):
            for x in range(40, 60):
                offset = (y * 200 + x) * 3
                second_rgb[offset:offset + 3] = b"\xff\x00\x00"
        link = FakeWindowLink(dict(WINDOW), first)
        capture = ScreenCapture(self.config, window_link=link)
        capture.capture()
        link.png = encode_png_rgb(200, 160, bytes(second_rgb))
        metadata, _ = capture.capture()
        diff = metadata["diff"]
        self.assertTrue(diff["changed"])
        self.assertEqual(diff["bbox_px"]["x"], 20)   # cropped image coordinates
        self.assertEqual(diff["bbox_px"]["y"], 20)
        self.assertEqual(diff["bbox_px"]["width"], 20)
        self.assertEqual(diff["bbox_px"]["height"], 10)


class WindowInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = window_config()
        self.backend = RecordingBackend()
        self.window_available = True

    def _provider(self) -> dict:
        if not self.window_available:
            return {"ok": False, "code": "window_not_found", "message": "no window matches"}
        return {"ok": True, "window": dict(WINDOW)}

    def _capture(self, window: dict, path: Path) -> None:
        Path(path).write_bytes(border_png(200, 160, 20))

    def _instance(self) -> TitellaInstance:
        link = WindowLink(
            self.config,
            window_provider=self._provider,
            capture_provider=self._capture,
            helper_provider=self.backend,
        )
        return TitellaInstance(self.config, link=link, firmware=FirmwareInfo(link))

    def test_kvm_status_reports_the_window_and_skips_bootstrap_advice(self) -> None:
        instance = self._instance()
        status = instance.kvm_status()
        self.assertTrue(status["firmware"]["reachable"])
        self.assertEqual(status["firmware"]["firmware"], "window-1.0")
        self.assertEqual(status["capture"]["source"], "window:Citrix Viewer")
        joined = " ".join(status["next_steps"])
        self.assertIn("lock_acquire", joined)
        # The bootstrap step is the one that deploys the agent; the lease step
        # may still name slave_bootstrap as a lease-gated tool.
        self.assertNotIn("The slave agent is not running", joined)
        self.assertNotIn("The slave platform is not selected", joined)

    def test_kvm_status_names_the_window_selectors_when_it_is_gone(self) -> None:
        self.window_available = False
        instance = self._instance()
        status = instance.kvm_status()
        self.assertFalse(status["firmware"]["reachable"])
        first = status["next_steps"][0]
        self.assertIn("window_app", first)
        self.assertIn("Screen Recording", first)
        joined = " ".join(status["next_steps"])
        self.assertNotIn("The slave agent is not running", joined)
        self.assertNotIn("The slave platform is not selected", joined)

    def test_ready_window_instance_has_no_pending_step(self) -> None:
        instance = self._instance()
        instance.capture.capture()
        instance.lease.acquire("ops", 600)
        status = instance.kvm_status()
        self.assertEqual(
            status["next_steps"],
            ["Everything is ready: lease, agent and capture are current."],
        )

    def test_screen_mouse_type_and_key_through_the_instance(self) -> None:
        instance = self._instance()
        instance.lease.acquire("ops", 600)
        metadata, image = instance.capture.capture()
        self.assertTrue(metadata["ok"])
        self.assertIsNotNone(image)
        self.assertEqual(metadata["source"], "window:4242")

        moved = instance.hid.mouse_abs("ops", 80, 60)
        self.assertTrue(moved["ok"], moved)
        self.assertEqual(moved["coords"]["input_px"], [80, 60])

        self.assertTrue(instance.hid.click("ops", "left")["ok"])
        self.assertTrue(instance.hid.wheel("ops", 2)["ok"])
        self.assertTrue(instance.hid.key("ops", "ENTER")["ok"])

        typed = instance.hid.type_text("ops", "ñ", enter=True)
        self.assertTrue(typed["ok"], typed)
        unicode_calls = [call for call in self.backend.calls if call[0] == "unicode"]
        self.assertEqual(len(unicode_calls), 1)
        self.assertEqual(
            base64.b64decode(
                unicode_calls[0][unicode_calls[0].index("--text-b64") + 1]
            ).decode("utf-8"),
            "ñ",
        )

    def test_type_rejects_a_character_the_layout_cannot_produce(self) -> None:
        instance = self._instance()
        instance.lease.acquire("ops", 600)
        result = instance.hid.type_text("ops", "🚀")
        self.assertEqual(result["error"]["code"], "untypeable_characters")
        self.assertEqual(self.backend.calls, [])

    def test_release_all_works_without_the_lease(self) -> None:
        instance = self._instance()
        instance.lease.acquire("ops", 600)
        instance.capture.capture()
        instance.hid.mouse_abs("ops", 80, 60, button="left", state="down")
        instance.lease.release("ops")
        released = instance.hid.release_all()
        self.assertTrue(released["ok"])
        self.assertEqual(released["released"], ["left"])

    def test_remote_shell_answers_cdc_unavailable(self) -> None:
        instance = self._instance()
        with self.assertRaises(TransportError) as ctx:
            instance.shells.remote_shell("echo hi")
        self.assertEqual(ctx.exception.code, "cdc_unavailable")
        with self.assertRaises(TransportError) as terminal:
            instance.shells.terminal_open(None, None, 24, 80, 60)
        self.assertEqual(terminal.exception.code, "cdc_unavailable")

    def test_files_and_transfers_answer_cdc_unavailable(self) -> None:
        instance = self._instance()
        gated = {
            "list": lambda: instance.files.list("~"),
            "sysinfo": lambda: instance.files.sysinfo(),
            "read": lambda: instance.files.read("~"),
            "screen": lambda: instance.files.screen(),
        }
        for name, call in gated.items():
            with self.subTest(call=name):
                try:
                    call()
                except TransportError as exc:
                    self.assertEqual(exc.code, "cdc_unavailable")
                else:
                    self.fail(f"files.{name} did not answer cdc_unavailable")
        # Transfer bookkeeping is local state and stays answerable.
        self.assertEqual(
            instance.files.transfer_status("unknown")["status"], "not_found"
        )

    def test_bootstrap_is_refused_before_any_keystroke(self) -> None:
        instance = self._instance()
        instance.lease.acquire("ops", 600)
        result = instance.bootstrap.bootstrap(
            "ops", "es_ES", platform="windows", layout_confirmed=True
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "cdc_unavailable")
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(instance.bootstrap.status()["status"], "not_started")

    def test_summary_and_upsert_round_trip_the_window_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(f'state_dir = "{root / "state"}"\n', encoding="utf-8")
            config = load_config(path, {})
            registry = TitellaRegistry(config, instance_factory=self._instance_factory)
            created = registry.upsert(
                "desk",
                transport="window",
                window_app="Citrix Viewer",
                window_title="remote session",
                set_default=True,
            )
            self.assertEqual(created["status"], "created")
            summary = created["titella"]
            self.assertEqual(summary["transport"], "window")
            self.assertEqual(summary["window_app"], "Citrix Viewer")
            self.assertEqual(summary["window_title"], "remote session")
            self.assertIsNone(summary["window_id"])
            self.assertIn('window_app = "Citrix Viewer"', path.read_text(encoding="utf-8"))

            # Retargeting rebuilds the instance instead of reloading in place.
            calls = {"built": 0}
            factory = self._instance_factory
            registry._factory = lambda cfg: calls.__setitem__("built", calls["built"] + 1) or factory(cfg)
            registry.upsert("desk", window_app="Other Viewer")
            self.assertEqual(calls["built"], 1)
            self.assertEqual(registry.resolve("desk").config.window_app, "Other Viewer")

    def test_upsert_without_a_window_selector_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(f'state_dir = "{root / "state"}"\n', encoding="utf-8")
            registry = TitellaRegistry(load_config(path, {}), instance_factory=self._instance_factory)
            with self.assertRaisesRegex(
                ValueError, "window_app, window_title or window_id is required"
            ):
                registry.upsert("desk", transport="window")

    def _instance_factory(self, config) -> TitellaInstance:
        link = WindowLink(
            config,
            window_provider=self._provider,
            capture_provider=self._capture,
            helper_provider=self.backend,
        )
        return TitellaInstance(config, link=link, firmware=FirmwareInfo(link))


class WindowDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = window_config()

    def _steps(self, **overrides) -> list[str]:
        values = {
            "firmware": {"reachable": True},
            "lease": {"ok": True, "locked": True, "owner": "ops", "expires_in": 600},
            "agent": {"status": "not_started"},
            "resolution": (800, 600),
            "capture_age_s": 1.0,
        }
        values.update(overrides)
        return next_steps(self.config, **values)

    def test_unreachable_window_is_the_first_step(self) -> None:
        steps = self._steps(firmware={"reachable": False})
        self.assertIn("window_app", steps[0])
        self.assertIn("window_title", steps[0])
        self.assertIn("window_id", steps[0])
        self.assertIn("Screen Recording", steps[0])
        self.assertNotIn("TITELLA_TRANSPORT", steps[0])

    def test_no_bootstrap_step_when_the_transport_is_a_window(self) -> None:
        steps = self._steps()
        self.assertEqual(
            steps, ["Everything is ready: lease, agent and capture are current."]
        )
        self.assertNotIn("slave_bootstrap", " ".join(steps))

    def test_lease_and_capture_steps_match_the_serial_transport(self) -> None:
        free = self._steps(lease={"ok": True, "locked": False})
        self.assertTrue(any("lock_acquire" in step for step in free))
        no_capture = self._steps(resolution=None, capture_age_s=None)
        self.assertTrue(any("screen()" in step for step in no_capture))

    def test_diagnose_reports_the_window_source(self) -> None:
        report = diagnose(
            self.config,
            firmware={"reachable": True},
            lease={"ok": True, "locked": True},
            agent={"status": "not_started"},
            resolution=(800, 600),
            capture_age_s=0.5,
        )
        self.assertEqual(report["capture"]["source"], "window:Citrix Viewer")
        self.assertEqual(report["capture"]["resolution"], [800, 600])


class AgentProtocolConfigTests(unittest.TestCase):
    def test_window_transport_defaults_to_none(self) -> None:
        self.assertEqual(window_config().agent_protocol, "none")

    def test_serial_and_wifi_default_to_serial(self) -> None:
        serial = load_config(environ={"TITELLA_STATE_DIR": _state_dir()})
        self.assertEqual(serial.agent_protocol, "serial")
        wifi = load_config(
            environ={"TITELLA_STATE_DIR": _state_dir(), "TITELLA_TRANSPORT": "wifi"}
        )
        self.assertEqual(wifi.agent_protocol, "serial")

    def test_every_protocol_value_is_accepted_from_the_environment(self) -> None:
        for value in ("serial", "clipboard", "visual", "none"):
            with self.subTest(value=value):
                config = window_config(TITELLA_AGENT_PROTOCOL=value)
                self.assertEqual(config.agent_protocol, value)

    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "agent_protocol must be serial, clipboard, visual or none"
        ):
            window_config(TITELLA_AGENT_PROTOCOL="carrier-pigeon")

    def test_explicit_protocol_wins_over_the_transport_default(self) -> None:
        config = window_config(TITELLA_AGENT_PROTOCOL="serial")
        self.assertEqual(config.transport, "window")
        self.assertEqual(config.agent_protocol, "serial")

    def test_toml_round_trip_keeps_the_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            save_titella_config(
                path,
                global_values={"state_dir": str(root / "state"), "http_port": 8765},
                titellas={
                    "vdi": {
                        "transport": "window",
                        "agent_protocol": "clipboard",
                        "window_app": "Citrix Viewer",
                    }
                },
                default_titella="vdi",
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn('agent_protocol = "clipboard"', text)
            vdi = load_config(path, {}).titellas["vdi"]
            self.assertEqual(vdi.agent_protocol, "clipboard")


class CdcChannelGuardTests(unittest.TestCase):
    def test_window_link_without_an_agent_channel_is_refused(self) -> None:
        link = WindowLink(window_config())
        with self.assertRaises(TransportError) as ctx:
            require_cdc_channel(link)
        self.assertEqual(ctx.exception.code, "cdc_unavailable")
        self.assertIn("window transport has no CDC channel", ctx.exception.message)
        self.assertIn("agent_protocol='clipboard'", ctx.exception.message)
        self.assertFalse(ctx.exception.retryable)
        link.close()

    def test_visual_protocol_names_the_missing_implementation(self) -> None:
        link = WindowLink(window_config(TITELLA_AGENT_PROTOCOL="visual"))
        with self.assertRaises(TransportError) as ctx:
            require_cdc_channel(link)
        self.assertEqual(ctx.exception.code, "cdc_unavailable")
        self.assertIn("not implemented", ctx.exception.message)
        link.close()

    def test_clipboard_protocol_passes_the_guard(self) -> None:
        link = WindowLink(window_config(TITELLA_AGENT_PROTOCOL="clipboard"))
        require_cdc_channel(link)
        link.close()

    def test_links_without_the_field_keep_their_native_channel(self) -> None:
        class NativeLink:
            transport = "serial"

        require_cdc_channel(NativeLink())


class SimulatedClipboard:
    """Plays both pasteboard sides: records writes and answers ``clip-read``.

    ``react`` runs right after a write, so a test can act as the slave and
    leave its answer where the next poll of the engine finds it.
    """

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.writes: list[tuple[list[str], str]] = []
        self.change_count = 0
        self.react = None

    def __call__(self, argv: list[str]) -> dict:
        if argv[0] == "clip-read":
            return {
                "ok": True,
                "change_count": self.change_count,
                "text_b64": base64.b64encode(self.text.encode("utf-8")).decode("ascii"),
            }
        if argv[0] == "clip-write":
            encoded = argv[argv.index("--text-b64") + 1]
            text = base64.b64decode(encoded).decode("utf-8")
            self.change_count += 1
            self.writes.append((list(argv), text))
            self.text = text
            if self.react is not None:
                self.react(text)
            return {"ok": True, "change_count": self.change_count}
        raise AssertionError(f"unexpected helper argv {argv}")

    def packets(self) -> list[str]:
        return [text for _argv, text in self.writes if text.startswith("TITELLA_M2S:")]

    def transient_flags(self) -> list[str]:
        return [
            argv[argv.index("--transient") + 1]
            for argv, _text in self.writes
            if "--transient" in argv
        ]


class ClipboardCdcTests(unittest.TestCase):
    """The master side of the clipboard channel, against a simulated pasteboard."""

    def setUp(self) -> None:
        self.config = window_config(TITELLA_AGENT_PROTOCOL="clipboard")
        self.clipboard = SimulatedClipboard(text="operator notes")
        self.link = WindowLink(self.config, clipboard_provider=self.clipboard)
        self.s2m_seq = 0

    def tearDown(self) -> None:
        self.link.close()

    def _respond(self, text: str) -> None:
        """Acts as the slave: answers each request frame with its response."""
        if not text.startswith("TITELLA_M2S:"):
            return
        header, _separator, body = text.partition("\n")
        parts = header.split(":")
        m2s_seq = int(parts[2])
        for line in body.split("\n"):
            if not line.strip():
                continue
            frame = json.loads(line)
            if frame.get("type") != "request":
                continue
            self.s2m_seq += 1
            response = {
                "v": 1, "channel": frame["channel"], "type": "response",
                "link_id": frame["link_id"], "request_id": frame["request_id"],
                "op": frame["op"], "body": {"ok": True, "echo": frame["op"]},
            }
            self.clipboard.text = (
                f"TITELLA_S2M:{frame['link_id']}:{self.s2m_seq}:{m2s_seq}\n"
                + json.dumps(response) + "\n"
            )

    def _wait_until(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_request_travels_as_a_packet_and_the_clipboard_is_restored(self) -> None:
        self.clipboard.react = self._respond
        response = self.link.request("shell", "job.start", {"cmd": "dir"}, timeout=5)
        self.assertTrue(response["body"]["ok"])
        self.assertEqual(response["body"]["echo"], "job.start")

        packets = self.clipboard.packets()
        self.assertTrue(packets, "no M2S packet was written")
        header, _separator, body = packets[0].partition("\n")
        self.assertEqual(
            header, f"TITELLA_M2S:{self.link.link_id}:1:0"
        )
        frame = json.loads(body.strip().split("\n")[0])
        self.assertEqual(frame["type"], "request")
        self.assertEqual(frame["channel"], "shell")
        self.assertEqual(frame["op"], "job.start")
        self.assertEqual(frame["body"], {"cmd": "dir"})
        # Packets are marked transient so clipboard history ignores them.
        self.assertIn("1", self.clipboard.transient_flags())

        self.assertTrue(
            self._wait_until(lambda: self.clipboard.text == "operator notes"),
            f"clipboard was not restored: {self.clipboard.text!r}",
        )
        # The restore writes the operator's text without the transient marker.
        self.assertEqual(self.clipboard.transient_flags()[-1], "0")

    def test_send_data_and_subscribers_share_the_packet_channel(self) -> None:
        received: list[dict] = []

        def on_shell(frame: dict) -> bool:
            received.append(frame)
            return True

        self.link.subscribe("shell", on_shell)
        session = "s1"

        def react(text: str) -> None:
            if not text.startswith("TITELLA_M2S:"):
                return
            header, _separator, body = text.partition("\n")
            parts = header.split(":")
            m2s_seq = int(parts[2])
            for line in body.split("\n"):
                if not line.strip():
                    continue
                frame = json.loads(line)
                if frame.get("type") != "data":
                    continue
                self.s2m_seq += 1
                ack = {
                    "v": 1, "channel": "shell", "type": "ack",
                    "link_id": frame["link_id"], "session_id": session,
                    "ack_seq": frame["seq"],
                }
                self.clipboard.text = (
                    f"TITELLA_S2M:{frame['link_id']}:{self.s2m_seq}:{m2s_seq}\n"
                    + json.dumps(ack) + "\n"
                )

        self.clipboard.react = react
        self.link.send_data("shell", session, 0, b"hello")
        sent = [
            json.loads(line)
            for packet in self.clipboard.packets()
            for line in packet.partition("\n")[2].split("\n")
            if line.strip()
        ]
        data_frames = [frame for frame in sent if frame.get("type") == "data"]
        self.assertEqual(len(data_frames), 1)
        self.assertEqual(data_frames[0]["seq"], 0)
        self.assertEqual(data_frames[0]["session_id"], session)

        # The slave now writes a data frame of its own; the subscriber accepts
        # it and the engine acknowledges that block automatically.
        self.clipboard.react = None
        self.s2m_seq += 1
        inbound = {
            "v": 1, "channel": "shell", "type": "data", "link_id": self.link.link_id,
            "session_id": session, "seq": 3,
            "data_b64": base64.b64encode(b"output").decode("ascii"),
        }
        self.clipboard.text = (
            f"TITELLA_S2M:{self.link.link_id}:{self.s2m_seq}:1\n"
            + json.dumps(inbound) + "\n"
        )
        self.assertTrue(self._wait_until(lambda: bool(received)), "no frame dispatched")
        self.assertEqual(received[0]["seq"], 3)
        self.assertTrue(
            self._wait_until(lambda: any(
                json.loads(line).get("type") == "ack"
                for packet in self.clipboard.packets()
                for line in packet.partition("\n")[2].split("\n")
                if line.strip()
            )),
            "the engine never acknowledged the inbound block",
        )
        self.assertTrue(
            self._wait_until(lambda: self.clipboard.text == "operator notes"),
            f"clipboard was not restored: {self.clipboard.text!r}",
        )

    def test_duplicate_packets_are_acknowledged_but_not_reapplied(self) -> None:
        received: list[dict] = []
        self.link.subscribe("shell", lambda frame: received.append(frame) or True)
        frame = {
            "v": 1, "channel": "shell", "type": "event", "link_id": self.link.link_id,
            "op": "job.finished", "body": {"status": "finished"},
        }
        packet = f"TITELLA_S2M:{self.link.link_id}:1:0\n{json.dumps(frame)}\n"
        # The unsolicited frame is expected because an operation declares that
        # it waits for one; that is the bootstrap stage handshake in practice.
        with self.link.clipboard_activity():
            self.clipboard.text = packet
            self.assertTrue(self._wait_until(lambda: len(received) == 1))
            # A re-write of the same packet (its ACK was lost) is not
            # re-dispatched, but its transport ACK is repeated.
            self.clipboard.text = packet
            time.sleep(0.2)
            self.assertEqual(len(received), 1)
            self.assertTrue(
                self._wait_until(lambda: any(
                    text.startswith("TITELLA_M2S:") and ":0:1" in text.split("\n")[0]
                    for text in self.clipboard.packets()
                )),
                "the engine never repeated the transport ACK",
            )

    def test_hid_requests_still_use_the_window_and_need_no_clipboard(self) -> None:
        backend = RecordingBackend()
        link = WindowLink(
            self.config,
            window_provider=lambda: {"ok": True, "window": dict(WINDOW)},
            helper_provider=backend,
            clipboard_provider=self.clipboard,
        )
        try:
            body = link.request("hid", "health", {})["body"]
            self.assertEqual(body["window"]["id"], 4242)
            self.assertEqual(self.clipboard.writes, [])
        finally:
            link.close()

    def test_no_idle_polling_before_the_agent_answers(self) -> None:
        reads = {"count": 0}
        original = self.clipboard.__call__

        def counting(argv: list[str]) -> dict:
            if argv[0] == "clip-read":
                reads["count"] += 1
            return original(argv)

        self.clipboard.__call__ = counting  # type: ignore[method-assign]
        time.sleep(0.3)
        self.assertEqual(reads["count"], 0, "the idle link read the pasteboard")

    def test_a_peer_packet_is_never_overwritten(self) -> None:
        """Half-duplex: the slot belongs to the peer until its packet is read.

        The clipboard has one slot and no retransmission buffer, so a body
        written while the slave's packet is still there destroys that packet for
        ever. The engine must keep its frames queued instead.
        """
        # The slave has answered something the master has not consumed yet.
        pending = {
            "v": 1, "channel": "shell", "type": "event", "link_id": self.link.link_id,
            "op": "job.finished", "body": {"status": "finished"},
        }
        self.clipboard.text = (
            f"TITELLA_S2M:{self.link.link_id}:7:0\n{json.dumps(pending)}\n"
        )
        before = list(self.clipboard.writes)
        self.assertFalse(
            self.link._peer_owns_slot() is False,
            "the guard did not detect the unread peer packet",
        )
        # A body write is refused and the frames stay queued.
        frame = {"v": 1, "channel": "shell", "type": "request", "link_id": self.link.link_id,
                 "request_id": 99, "op": "job.start", "body": {}}
        self.link._write_packet([frame])
        self.assertEqual(self.clipboard.writes, before, "the peer packet was overwritten")
        self.assertTrue(self.link._outbound, "the refused frames were dropped")

    def test_a_consumed_peer_packet_frees_the_slot(self) -> None:
        """Once the peer packet is read, writing is allowed again."""
        received: list[dict] = []
        self.link.subscribe("shell", lambda frame: received.append(frame) or True)
        frame = {
            "v": 1, "channel": "shell", "type": "event", "link_id": self.link.link_id,
            "op": "job.finished", "body": {"status": "finished"},
        }
        self.clipboard.text = (
            f"TITELLA_S2M:{self.link.link_id}:3:0\n{json.dumps(frame)}\n"
        )
        with self.link.clipboard_activity():
            self.assertTrue(self._wait_until(lambda: len(received) == 1))
        self.assertFalse(self.link._peer_owns_slot(), "the slot stayed locked after the read")


class ClipboardBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        # The clipboard protocol has no serial device at all: an inherited
        # slave_cdc_port (this machine's config defaults to the physical rig)
        # is rejected before any keystroke, so it is cleared here.
        self.config = window_config(
            TITELLA_AGENT_PROTOCOL="clipboard",
            TITELLA_SLAVE_PLATFORM="windows",
            TITELLA_SLAVE_CDC_PORT="",
        )
        self.clipboard = SimulatedClipboard()
        self.link = WindowLink(self.config, clipboard_provider=self.clipboard)
        hid = HidController(self.link, HidLease(self.config), ScreenCapture(self.config))
        self.manager = BootstrapManager(self.config, self.link, hid)

    def tearDown(self) -> None:
        self.link.close()

    def test_clipboard_bootstrap_accepts_windows_and_rejects_a_cdc_port(self) -> None:
        result = self.manager.bootstrap(
            "ops", "es_ES", platform="windows", cdc_port="COM3", layout_confirmed=True
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_argument")
        self.assertIn("clipboard", result["error"]["message"])

    def test_clipboard_bootstrap_rejects_a_non_windows_platform(self) -> None:
        result = self.manager.bootstrap(
            "ops", "es_ES", platform="linux", layout_confirmed=True
        )
        self.assertEqual(result["error"]["code"], "invalid_argument")
        self.assertIn("windows", result["error"]["message"])

    def test_stub_command_is_ascii_small_and_speaks_the_protocol(self) -> None:
        link_id = "0123456789abcdef0123456789abcdef"
        stub = windows_clipboard_stub_command(link_id)
        self.assertTrue(stub.isascii(), "the stub must survive the HID layout")
        # 919 characters: the stub carries the loader protocol plus the CRLF
        # normalisation the clipboard hop makes mandatory. It is clipboard text,
        # never typed, so its size is not a keystroke-budget concern.
        self.assertLess(len(stub), 1000, f"stub is {len(stub)} characters")
        self.assertIn("Set-Clipboard", stub)
        self.assertIn("Get-Clipboard", stub)
        self.assertIn("TITELLA_S2M:", stub)
        self.assertIn("TITELLA_M2S:", stub)
        self.assertIn(link_id, stub)
        self.assertIn("$script:Pin='CLIPBOARD'", stub)
        # A typed line must not contain a literal newline: it is one command.
        self.assertNotIn("\n", stub)
        with self.assertRaisesRegex(ValueError, "link_id"):
            windows_clipboard_stub_command("not-a-link")

    def test_stage1_is_a_short_bracket_free_launcher(self) -> None:
        # The whole point of the clipboard protocol: what the keyboard carries
        # is only this line, so a client that mangles brackets (XQuartz turns
        # `[` into a backtick) cannot corrupt the stub.
        self.assertEqual(CLIPBOARD_STAGE1, "iex (Get-Clipboard -Raw)")
        self.assertLess(len(CLIPBOARD_STAGE1), 32)
        self.assertTrue(CLIPBOARD_STAGE1.isascii())
        for ch in "[]{},":
            self.assertNotIn(ch, CLIPBOARD_STAGE1)

    def test_bootstrap_publishes_the_stub_and_types_only_stage1(self) -> None:
        backend = RecordingBackend()
        self.link._helper_provider = backend  # type: ignore[attr-defined]
        self.link._window_provider = lambda: {"ok": True, "window": dict(WINDOW)}  # type: ignore[attr-defined]
        self.manager.hid.lease.acquire("ops", 600)
        try:
            self.manager.bootstrap(
                "ops", "es_ES", platform="windows",
                layout_confirmed=True, skip_launch=True,
            )
        finally:
            self.link._helper_provider = None  # type: ignore[attr-defined]
            self.link._window_provider = None  # type: ignore[attr-defined]

        stubs = [
            text for _argv, text in self.clipboard.writes
            if "CLIPBOARD" in text and "TITELLA_S2M:" in text
        ]
        self.assertEqual(len(stubs), 1, "the stub was not published to the clipboard")
        self.assertIn("$script:Pin='CLIPBOARD'", stubs[0])

        typed = "".join(
            base64.b64decode(call[call.index("--text-b64") + 1]).decode("utf-8")
            for call in backend.calls
            if call[0] == "unicode" and "--text-b64" in call
        )
        self.assertEqual(typed, CLIPBOARD_STAGE1)
        self.assertNotIn("Set-Clipboard", typed, "the stub must never be typed")


class ClipboardDiagnosticsTests(unittest.TestCase):
    def test_clipboard_protocol_adds_the_bootstrap_step(self) -> None:
        config = window_config(TITELLA_AGENT_PROTOCOL="clipboard")
        steps = next_steps(
            config,
            firmware={"reachable": True},
            lease={"ok": True, "locked": True},
            agent={"status": "not_started"},
            resolution=(800, 600),
            capture_age_s=1.0,
        )
        self.assertTrue(any("slave_bootstrap" in step for step in steps))

    def test_window_default_keeps_omitting_the_bootstrap_step(self) -> None:
        config = window_config()
        steps = next_steps(
            config,
            firmware={"reachable": True},
            lease={"ok": True, "locked": True},
            agent={"status": "not_started"},
            resolution=(800, 600),
            capture_age_s=1.0,
        )
        self.assertEqual(
            steps, ["Everything is ready: lease, agent and capture are current."]
        )

    def test_diagnose_reports_the_agent_protocol(self) -> None:
        report = diagnose(
            window_config(TITELLA_AGENT_PROTOCOL="clipboard"),
            firmware={"reachable": True},
            lease={"ok": True, "locked": True},
            agent={"status": "not_started"},
            resolution=(800, 600),
            capture_age_s=0.5,
        )
        self.assertEqual(report["agent_protocol"], "clipboard")


class ClipboardRegistryTests(unittest.TestCase):
    def _factory(self, config):
        link = WindowLink(
            config,
            window_provider=lambda: {"ok": True, "window": dict(WINDOW)},
            helper_provider=RecordingBackend(),
            clipboard_provider=SimulatedClipboard(),
        )
        return TitellaInstance(config, link=link, firmware=FirmwareInfo(link))

    def test_upsert_persists_and_reports_the_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(f'state_dir = "{root / "state"}"\n', encoding="utf-8")
            registry = TitellaRegistry(load_config(path, {}), instance_factory=self._factory)
            created = registry.upsert(
                "vdi",
                transport="window",
                agent_protocol="clipboard",
                window_app="Citrix Viewer",
                set_default=True,
            )
            self.assertEqual(created["status"], "created")
            self.assertEqual(created["titella"]["agent_protocol"], "clipboard")
            self.assertIn('agent_protocol = "clipboard"', path.read_text(encoding="utf-8"))

            window_default = registry.upsert("desk", transport="window", window_app="Other")
            self.assertEqual(window_default["titella"]["agent_protocol"], "none")
            # The transport default is never pinned into the table.
            self.assertNotIn('agent_protocol = "none"', path.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(
                ValueError, "agent_protocol must be serial, clipboard, visual or none"
            ):
                registry.upsert("desk", agent_protocol="telepathy")
            registry.close_all()


if __name__ == "__main__":
    unittest.main()
