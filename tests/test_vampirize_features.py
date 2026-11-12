"""Regression tests for the four-level keymap, panic release, mouse holds,
UVC auto-crop, screen diff, measure_diff and the configurable LLM OCR.

The fake link records every ``hid`` request instead of touching a device, so
the bodies asserted here are exactly what the firmware would receive.
"""
from __future__ import annotations

import base64
import json
import struct
import subprocess
import tempfile
import threading
import unittest
import zlib
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from titella.config import load_config
from titella.firmware_info import FirmwareInfo
from titella.hid import HidController, HidLease, RAW_STEPS_PER_FRAME
from titella.keymap import (
    encode_steps,
    firmware_direct_text,
    normalize_text,
    unreachable_characters,
)
from titella.mcp_server import measure_screen_diff
from titella.ocr import DEFAULT_OCR_PROMPT, run_ocr
from titella.png_codec import (
    compute_diff_bbox,
    crop_rgb,
    decode_png,
    detect_black_borders,
    encode_png_rgb,
)
from titella.registry import TitellaInstance, TitellaRegistry
from titella.screen import ScreenCapture, png_resolution


class RecordingLink:
    """SerialLink stand-in that answers every request and keeps the bodies."""

    def __init__(self) -> None:
        self.link_id = "0123456789abcdef0123456789abcdef"
        self.connected = True
        self.state = "connected"
        self.port: str | None = "fake"
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.handlers: list[Any] = []
        self.state_handlers: list[Any] = []
        self.reply: dict[str, Any] = {"body": {"ok": True, "status": "hid_ready"}}

    def start(self) -> None:
        return None

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return True

    def subscribe(self, channel: str, handler: Any, session_id: str | None = None):
        self.handlers.append((channel, handler, session_id))
        return lambda: None

    def subscribe_state(self, handler: Any):
        self.state_handlers.append(handler)
        handler(self.state)
        return lambda: self.state_handlers.remove(handler)

    def request(
        self,
        channel: str,
        op: str,
        body: dict[str, Any] | None = None,
        timeout: float = 6.0,
    ) -> dict[str, Any]:
        self.requests.append((channel, op, dict(body or {})))
        if channel == "control" and op == "bind":
            return {"body": {"status": "ready", "link_id": self.link_id}}
        return self.reply

    def close(self, clean: bool = True) -> None:
        return None

    def hid_calls(self) -> list[tuple[str, dict[str, Any]]]:
        return [(op, body) for channel, op, body in self.requests if channel == "hid"]


class VampirizeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = replace(
            load_config(
                self.root / "absent.toml",
                {"TITELLA_STATE_DIR": str(self.root / "state")},
            ),
            screen_device="Fake card",
        )
        self.link = RecordingLink()
        self.capture = ScreenCapture(self.config)
        self.lease = HidLease(self.config)
        self.lease.acquire("ops", 600)
        self.hid = HidController(self.link, self.lease, self.capture, layout="es_ES")

    def seed_frame(self, width: int = 64, height: int = 48) -> bytes:
        image = encode_png_rgb(width, height, bytes(width * height * 3))
        metadata, _ = self.capture.record(image, source="uvc")
        self.assertTrue(metadata["ok"], metadata)
        return image


# ── 1. four-level keymap and whole-string pre-validation ────────────────
class KeymapTests(unittest.TestCase):
    def test_es_es_covers_four_levels_dead_keys_and_iso_key(self) -> None:
        expected = {
            "ñ": ["00:33"],
            "Ñ": ["02:33"],
            "ç": ["00:31"],
            "Ç": ["02:31"],
            "€": ["40:08"],
            "¡": ["00:2e"],
            "¿": ["02:2e"],
            "º": ["00:35"],
            "ª": ["02:35"],
            "·": ["02:20"],
            "<": ["00:64"],
            ">": ["02:64"],
            "~": ["40:21", "00:2c"],
            "^": ["02:2f", "00:2c"],
            "`": ["00:2f", "00:2c"],
            "´": ["00:34", "00:2c"],
            "¨": ["02:34", "00:2c"],
            "á": ["00:34", "00:04"],
            "é": ["00:34", "00:08"],
            "í": ["00:34", "00:0c"],
            "ó": ["00:34", "00:12"],
            "ú": ["00:34", "00:18"],
            "Á": ["00:34", "02:04"],
            "à": ["00:2f", "00:04"],
            "â": ["02:2f", "00:04"],
            "ä": ["02:34", "00:04"],
        }
        for char, steps in expected.items():
            with self.subTest(char=char):
                self.assertEqual(encode_steps(char, "es_ES"), steps)

    def test_en_us_is_plain_and_shift_only(self) -> None:
        self.assertEqual(encode_steps("<", "en_US"), ["02:36"])
        self.assertEqual(encode_steps(">", "en_US"), ["02:37"])
        self.assertEqual(encode_steps("~", "en_US"), ["02:35"])
        self.assertEqual(encode_steps("`", "en_US"), ["00:35"])
        self.assertEqual(encode_steps("^", "en_US"), ["02:23"])
        self.assertEqual(unreachable_characters("café", "en_US"), ["é"])
        self.assertEqual(unreachable_characters("ñ", "en_US"), ["ñ"])

    def test_unreachable_list_is_complete_unique_and_ordered(self) -> None:
        self.assertEqual(unreachable_characters("aΩbΩc", "es_ES"), ["Ω"])
        self.assertEqual(unreachable_characters("ΩΩЖ", "es_ES"), ["Ω", "Ж"])
        self.assertEqual(unreachable_characters("ok", "es_ES"), [])

    def test_firmware_direct_text_matches_the_ascii_maps(self) -> None:
        self.assertTrue(firmware_direct_text("hola 123!", "es_ES"))
        self.assertTrue(firmware_direct_text("line\nnext\ttab", "es_ES"))
        for char in "`^~":
            with self.subTest(char=char):
                self.assertFalse(firmware_direct_text(f"a{char}b", "es_ES"))
                self.assertTrue(firmware_direct_text(f"a{char}b", "en_US"))
        self.assertFalse(firmware_direct_text("cañón", "es_ES"))
        self.assertFalse(firmware_direct_text("", "es_ES"))

    def test_line_endings_are_normalized(self) -> None:
        self.assertEqual(normalize_text("a\r\nb\rc\nd"), "a\nb\nc\nd")
        self.assertEqual(encode_steps("a\r\nb", "es_ES"), ["00:04", "00:28", "00:05"])


class TypeTextTests(VampirizeTestCase):
    def test_pure_ascii_stays_on_kb_text(self) -> None:
        result = self.hid.type_text("ops", "hello", 10)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.link.hid_calls(),
            [("kb_text", {"text": "hello", "delay": 10, "enter": False})],
        )

    def test_enter_flag_is_forwarded_on_both_paths(self) -> None:
        self.hid.type_text("ops", "hi", 5, enter=True)
        self.assertEqual(
            self.link.hid_calls()[-1],
            ("kb_text", {"text": "hi", "delay": 5, "enter": True}),
        )
        self.link.requests.clear()
        self.hid.type_text("ops", "ñ", 7, enter=True)
        self.assertEqual(
            self.link.hid_calls(),
            [("kb_raw", {"steps": ["00:33"], "delay": 7, "enter": True})],
        )

    def test_extended_characters_use_kb_raw_steps(self) -> None:
        self.hid.type_text("ops", "cañón €~^`áç<>", 3)
        calls = self.link.hid_calls()
        self.assertEqual(len(calls), 1, calls)
        op, body = calls[0]
        self.assertEqual(op, "kb_raw")
        self.assertEqual(body["delay"], 3)
        self.assertFalse(body["enter"])
        self.assertEqual(
            body["steps"],
            encode_steps("cañón €~^`áç<>", "es_ES"),
        )
        # The raw path expresses what the ASCII map cannot.
        for step in ("00:33", "40:08", "40:21", "02:2f", "00:2f", "00:34", "00:31", "00:64"):
            self.assertIn(step, body["steps"])

    def test_untypeable_text_rejects_before_any_frame(self) -> None:
        result = self.hid.type_text("ops", "ok Ω ok", 10, enter=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "untypeable_characters")
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(result["layout"], "es_ES")
        self.assertEqual(result["unreachable"], ["Ω"])
        self.assertEqual(self.link.requests, [])

    def test_en_us_rejects_extended_characters(self) -> None:
        self.hid.keyboard_layout("ops", "en_US")
        result = self.hid.type_text("ops", "café")
        self.assertEqual(result["error"]["code"], "untypeable_characters")
        self.assertEqual(result["unreachable"], ["é"])
        self.assertEqual(result["layout"], "en_US")
        # Switching back restores the Spanish table for validation.
        self.hid.keyboard_layout("ops", "es_ES")
        self.assertTrue(self.hid.type_text("ops", "café")["ok"])

    def test_long_raw_text_is_chunked(self) -> None:
        text = "ñ" * (RAW_STEPS_PER_FRAME * 2 + 5)
        result = self.hid.type_text("ops", text, 1, enter=True)
        self.assertTrue(result["ok"], result)
        calls = self.link.hid_calls()
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(op == "kb_raw" for op, _ in calls))
        self.assertEqual([len(body["steps"]) for _, body in calls],
                         [RAW_STEPS_PER_FRAME, RAW_STEPS_PER_FRAME, 5])
        # Enter rides on the last chunk only.
        self.assertEqual([body["enter"] for _, body in calls], [False, False, True])

    def test_bootstrap_positional_signature_still_works(self) -> None:
        # bootstrap.py calls type_text(owner, text, delay) positionally.
        result = self.hid.type_text("ops", "stub --flag", 4)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.link.hid_calls()[-1][1]["delay"], 4)


# ── 2. release_all and mouse press/hold/release ─────────────────────────
class ReleaseAndMouseTests(VampirizeTestCase):
    def test_release_all_ignores_the_lease(self) -> None:
        # No lease at all: the emergency stop must still reach the firmware.
        self.lease.release("ops")
        result = self.hid.release_all()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.link.hid_calls(), [("release_all", {})])

    def test_release_all_works_while_another_owner_holds_the_lease(self) -> None:
        self.lease.release("ops")
        self.lease.acquire("someone-else", 600)
        result = self.hid.release_all()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.link.hid_calls()[-1], ("release_all", {}))

    def test_click_states_keep_the_default_body_stable(self) -> None:
        self.hid.click("ops", "left", False)
        self.assertEqual(
            self.link.hid_calls()[-1],
            ("mouse_click", {"button": "left", "double": False}),
        )
        self.hid.click("ops", "right", True, state="down")
        self.assertEqual(
            self.link.hid_calls()[-1],
            ("mouse_click", {"button": "right", "double": True, "state": "down"}),
        )
        self.hid.click("ops", "middle", False, state="up")
        self.assertEqual(
            self.link.hid_calls()[-1],
            ("mouse_click", {"button": "middle", "double": False, "state": "up"}),
        )
        invalid = self.hid.click("ops", "left", False, state="hold")
        self.assertEqual(invalid["error"]["code"], "invalid_argument")

    def test_mouse_moves_default_body_is_unchanged(self) -> None:
        self.seed_frame(64, 48)
        self.hid.mouse_abs("ops", 10, 20)
        self.assertEqual(
            self.link.hid_calls()[-1],
            ("mouse_abs", {"x": round(10 * 32767 / 63), "y": round(20 * 32767 / 47)}),
        )
        self.hid.mouse_rel("ops", 3, -2)
        self.assertEqual(self.link.hid_calls()[-1], ("mouse_rel", {"x": 3, "y": -2}))

    def test_mouse_moves_carry_button_and_state(self) -> None:
        self.seed_frame(64, 48)
        self.hid.mouse_abs("ops", 10, 20, button="right", state="down")
        op, body = self.link.hid_calls()[-1]
        self.assertEqual(op, "mouse_abs")
        self.assertEqual(body["button"], "right")
        self.assertEqual(body["state"], "down")
        self.assertEqual(body["x"], round(10 * 32767 / 63))

        self.hid.mouse_rel("ops", 5, 5, state="up")
        self.assertEqual(
            self.link.hid_calls()[-1],
            ("mouse_rel", {"x": 5, "y": 5, "button": "left", "state": "up"}),
        )
        # keep + button presses before the move, so a drag needs no down state.
        self.hid.mouse_rel("ops", 1, 1, button="middle")
        self.assertEqual(
            self.link.hid_calls()[-1],
            ("mouse_rel", {"x": 1, "y": 1, "button": "middle", "state": "keep"}),
        )

    def test_mouse_arguments_are_validated(self) -> None:
        self.seed_frame()
        self.assertEqual(
            self.hid.mouse_rel("ops", 1, 1, button="side")["error"]["code"],
            "invalid_argument",
        )
        self.assertEqual(
            self.hid.mouse_abs("ops", 1, 1, state="hold")["error"]["code"],
            "invalid_argument",
        )


# ── 3. auto-crop, diff, screen_diff and measure_diff ────────────────────
def make_rgb(width: int, height: int, fill: int = 0) -> bytearray:
    return bytearray(bytes((fill, fill, fill)) * (width * height))


class PngCodecTests(unittest.TestCase):
    def _filtered_png(self, width: int, height: int, rgb: bytes, filters: list[int]) -> bytes:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        row_bytes = width * 3
        raw = bytearray()
        previous = bytes(row_bytes)
        for row in range(height):
            current = rgb[row * row_bytes:(row + 1) * row_bytes]
            filter_type = filters[row % len(filters)]
            line = bytearray(row_bytes)
            for index in range(row_bytes):
                left = current[index - 3] if index >= 3 else 0
                up = previous[index]
                up_left = previous[index - 3] if index >= 3 else 0
                if filter_type == 0:
                    value = current[index]
                elif filter_type == 1:
                    value = (current[index] - left) & 0xFF
                elif filter_type == 2:
                    value = (current[index] - up) & 0xFF
                elif filter_type == 3:
                    value = (current[index] - ((left + up) >> 1)) & 0xFF
                else:
                    estimate = left + up - up_left
                    distances = (
                        abs(estimate - left), abs(estimate - up), abs(estimate - up_left),
                    )
                    predictor = (
                        left if distances[0] <= distances[1] and distances[0] <= distances[2]
                        else up if distances[1] <= distances[2]
                        else up_left
                    )
                    value = (current[index] - predictor) & 0xFF
                line[index] = value
            raw.append(filter_type)
            raw += line
            previous = current
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b"")
        )

    def test_all_five_scanline_filters_decode(self) -> None:
        width, height = 17, 9
        rgb = bytes((index * 37 + 11) % 256 for index in range(width * height * 3))
        for filters in ([0], [1], [2], [3], [4], [0, 1, 2, 3, 4]):
            with self.subTest(filters=filters):
                decoded = decode_png(self._filtered_png(width, height, rgb, filters))
                self.assertEqual(decoded, (width, height, rgb))

    def test_grayscale_palette_and_alpha_color_types(self) -> None:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        def png(color_type: int, pixel: bytes, palette: bytes | None = None) -> bytes:
            width, height = 2, 1
            body = b"\x00" + pixel
            return (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
                + (chunk(b"PLTE", palette) if palette is not None else b"")
                + chunk(b"IDAT", zlib.compress(body))
                + chunk(b"IEND", b"")
            )

        # 0: grayscale
        self.assertEqual(decode_png(png(0, b"\x10\x20"))[2], bytes((16, 16, 16, 32, 32, 32)))
        # 4: grayscale+alpha (alpha dropped)
        self.assertEqual(decode_png(png(4, b"\x10\xff\x20\x80"))[2], bytes((16, 16, 16, 32, 32, 32)))
        # 6: RGBA (alpha dropped)
        self.assertEqual(
            decode_png(png(6, b"\x01\x02\x03\xff\x04\x05\x06\x80"))[2],
            bytes((1, 2, 3, 4, 5, 6)),
        )
        # 3: palette
        self.assertEqual(
            decode_png(png(3, b"\x01\x00", palette=b"\x00\x00\x00\xff\x00\x00"))[2],
            bytes((255, 0, 0, 0, 0, 0)),
        )

    def test_crop_and_bounds(self) -> None:
        width, height = 12, 8
        rgb = bytes((index % 251) for index in range(width * height * 3))
        cropped = crop_rgb(width, height, rgb, 3, 2, 4, 3)
        self.assertEqual(len(cropped), 4 * 3 * 3)
        self.assertEqual(cropped[:12], rgb[(2 * width + 3) * 3:(2 * width + 3) * 3 + 12])
        with self.assertRaises(ValueError):
            crop_rgb(width, height, rgb, 10, 0, 4, 2)
        with self.assertRaises(ValueError):
            crop_rgb(width, height, rgb, 0, 0, 0, 2)

    def test_detect_black_borders_pillarbox_and_letterbox(self) -> None:
        # 200x80 with 50 px black columns on both sides.
        rgb = make_rgb(200, 80)
        for y in range(80):
            for x in range(50, 150):
                offset = (y * 200 + x) * 3
                rgb[offset:offset + 3] = b"\xc8\x32\x1e"
        self.assertEqual(detect_black_borders(200, 80, bytes(rgb)), (50, 0, 100, 80))
        # 120x200 with 60 px black rows above and below.
        rgb = make_rgb(120, 200)
        for y in range(60, 140):
            for x in range(120):
                offset = (y * 120 + x) * 3
                rgb[offset:offset + 3] = b"\x0a\xc8\x1e"
        self.assertEqual(detect_black_borders(120, 200, bytes(rgb)), (0, 60, 120, 80))

    def test_detect_black_borders_never_crops_a_dark_or_tiny_frame(self) -> None:
        # A 100% black "no signal" frame.
        self.assertIsNone(detect_black_borders(200, 120, bytes(200 * 120 * 3)))
        # Content below the 64x64 floor.
        rgb = make_rgb(200, 120)
        for y in range(60, 62):
            for x in range(100, 102):
                offset = (y * 200 + x) * 3
                rgb[offset:offset + 3] = b"\xff\xff\xff"
        self.assertIsNone(detect_black_borders(200, 120, bytes(rgb)))
        # A border thinner than the 4 px minimum.
        rgb = make_rgb(200, 100)
        for y in range(2, 98):
            for x in range(2, 198):
                offset = (y * 200 + x) * 3
                rgb[offset:offset + 3] = b"\x40\x40\x40"
        self.assertIsNone(detect_black_borders(200, 100, bytes(rgb)))

    def test_diff_bbox_keeps_the_noise_floor(self) -> None:
        width, height = 30, 10
        previous = bytes(width * height * 3)
        noisy = bytearray(previous)
        for offset in range(0, len(noisy), 3):
            noisy[offset] = 12  # exactly at the floor: not a change
        self.assertFalse(compute_diff_bbox(width, height, previous, bytes(noisy))["changed"])

        changed = bytearray(previous)
        for y in range(3, 5):
            for x in range(7, 9):
                offset = (y * width + x) * 3
                changed[offset:offset + 3] = b"\xff\x00\x00"
        changed[0] = 13  # one pixel above the floor at (0, 0)
        diff = compute_diff_bbox(width, height, previous, bytes(changed))
        self.assertTrue(diff["changed"])
        self.assertEqual(diff["bbox_px"], {"x": 0, "y": 0, "width": 9, "height": 5})
        self.assertEqual(diff["changed_pixels"], 5)
        self.assertEqual(diff["diff_ratio"], round(5 / (width * height), 6))


class ScreenCropAndDiffTests(VampirizeTestCase):
    def test_uvc_capture_is_cropped_to_the_content_box(self) -> None:
        rgb = make_rgb(200, 80)
        for y in range(80):
            for x in range(50, 150):
                offset = (y * 200 + x) * 3
                rgb[offset:offset + 3] = b"\xc8\x32\x1e"
        frame = encode_png_rgb(200, 80, bytes(rgb))
        metadata, image = self.capture_uvc(frame)
        self.assertTrue(metadata["ok"], metadata)
        self.assertEqual(metadata["crop_box"], {"x": 50, "y": 0, "width": 100, "height": 80})
        self.assertEqual(metadata["raw_resolution"], {"width_px": 200, "height_px": 80})
        self.assertEqual(metadata["capture_resolution"], {"width_px": 100, "height_px": 80})
        self.assertIsNotNone(image)
        self.assertEqual(png_resolution(image), (100, 80))
        # mouse_abs calibrates against the cropped resolution.
        self.assertEqual(self.capture.resolution(), (100, 80))

    def test_all_black_uvc_frame_is_never_cropped(self) -> None:
        frame = encode_png_rgb(120, 90, bytes(120 * 90 * 3))
        metadata, image = self.capture_uvc(frame)
        self.assertTrue(metadata["ok"], metadata)
        self.assertNotIn("crop_box", metadata)
        self.assertEqual(metadata["capture_resolution"], {"width_px": 120, "height_px": 90})
        self.assertEqual(image, frame)
        self.assertEqual(self.capture.resolution(), (120, 90))

    def test_autocrop_can_be_disabled(self) -> None:
        self.capture.config = replace(self.config, screen_autocrop=False)
        rgb = make_rgb(200, 80)
        for y in range(80):
            for x in range(50, 150):
                offset = (y * 200 + x) * 3
                rgb[offset:offset + 3] = b"\xc8\x32\x1e"
        frame = encode_png_rgb(200, 80, bytes(rgb))
        metadata, image = self.capture_uvc(frame)
        self.assertNotIn("crop_box", metadata)
        self.assertEqual(image, frame)

    def capture_uvc(self, frame: bytes) -> tuple[dict, bytes | None]:
        class Completed:
            returncode = 0
            stdout = frame
            stderr = b""

        original = subprocess.run
        subprocess.run = lambda *args, **kwargs: Completed()  # type: ignore[assignment]
        try:
            return self.capture.capture()
        finally:
            subprocess.run = original  # type: ignore[assignment]

    def test_record_reports_baseline_then_diff(self) -> None:
        first = encode_png_rgb(24, 16, bytes(24 * 16 * 3))
        baseline, _ = self.capture.record(first, source="uvc")
        self.assertTrue(baseline["diff"]["baseline"])
        self.assertFalse(baseline["unchanged_since_previous"])
        self.assertEqual(len(baseline["frame_sha256"]), 64)

        changed = bytearray(24 * 16 * 3)
        for y in range(4, 6):
            for x in range(6, 9):
                offset = (y * 24 + x) * 3
                changed[offset:offset + 3] = b"\x00\xff\x00"
        second, _ = self.capture.record(encode_png_rgb(24, 16, bytes(changed)), source="uvc")
        self.assertFalse(second["unchanged_since_previous"])
        self.assertTrue(second["diff"]["changed"])
        self.assertEqual(second["diff"]["bbox_px"], {"x": 6, "y": 4, "width": 3, "height": 2})
        self.assertTrue(self.capture.prev_frame_path.is_file())

        repeated, _ = self.capture.record(encode_png_rgb(24, 16, bytes(changed)), source="uvc")
        self.assertTrue(repeated["unchanged_since_previous"])
        self.assertFalse(repeated["diff"]["changed"])
        self.assertIsNone(repeated["diff"]["bbox_px"])

    def test_resolution_change_reports_the_full_frame(self) -> None:
        self.capture.record(encode_png_rgb(24, 16, bytes(24 * 16 * 3)), source="uvc")
        metadata, _ = self.capture.record(
            encode_png_rgb(10, 8, bytes(10 * 8 * 3)), source="uvc"
        )
        self.assertTrue(metadata["diff"]["resolution_changed"])
        self.assertEqual(metadata["diff"]["diff_ratio"], 1.0)

    def test_screen_diff_crops_a_padded_and_clamped_png(self) -> None:
        self.assertEqual(
            self.capture.screen_diff()[0]["error"]["code"], "screen_required"
        )
        self.capture.record(encode_png_rgb(40, 20, bytes(40 * 20 * 3)), source="uvc")
        changed = bytearray(40 * 20 * 3)
        for y in range(5, 9):
            for x in range(10, 14):
                offset = (y * 40 + x) * 3
                changed[offset:offset + 3] = b"\xee\x11\x22"
        self.capture.record(encode_png_rgb(40, 20, bytes(changed)), source="uvc")

        metadata, image = self.capture.screen_diff(padding_px=2)
        self.assertTrue(metadata["ok"], metadata)
        self.assertTrue(metadata["changed"])
        self.assertEqual(metadata["raw_bbox_px"], {"x": 10, "y": 5, "width": 4, "height": 4})
        self.assertEqual(metadata["bbox_px"], {"x": 8, "y": 3, "width": 8, "height": 8})
        self.assertIsNotNone(image)
        self.assertEqual(png_resolution(image), (8, 8))
        self.assertEqual(decode_png(image)[2], crop_rgb(40, 20, bytes(changed), 8, 3, 8, 8))

        # Padding is clamped at the frame edge instead of leaving it.
        self.capture.record(encode_png_rgb(40, 20, bytes(40 * 20 * 3)), source="uvc")
        edge = bytearray(40 * 20 * 3)
        edge[0:3] = b"\xff\xff\xff"
        self.capture.record(encode_png_rgb(40, 20, bytes(edge)), source="uvc")
        metadata, image = self.capture.screen_diff(padding_px=16)
        self.assertEqual(metadata["bbox_px"], {"x": 0, "y": 0, "width": 17, "height": 17})
        self.assertEqual(png_resolution(image), (17, 17))

    def test_screen_diff_without_a_change_returns_no_image(self) -> None:
        image = encode_png_rgb(20, 10, bytes(20 * 10 * 3))
        self.capture.record(image, source="uvc")
        self.capture.record(image, source="uvc")
        metadata, crop = self.capture.screen_diff()
        self.assertFalse(metadata["changed"])
        self.assertIsNone(crop)

    def test_measure_diff_attaches_the_capture_envelope(self) -> None:
        self.seed_frame(64, 48)
        changed = bytearray(64 * 48 * 3)
        changed[0:3] = b"\xff\x00\x00"
        frame = encode_png_rgb(64, 48, bytes(changed))
        inst = SimpleNamespace(
            capture=self.capture,
            gate=SimpleNamespace(ready=False),
            files=None,
        )
        original = subprocess.run

        class Completed:
            returncode = 0
            stdout = frame
            stderr = b""

        subprocess.run = lambda *args, **kwargs: Completed()  # type: ignore[assignment]
        try:
            result = measure_screen_diff(inst, {"ok": True}, settle_ms=0)
        finally:
            subprocess.run = original  # type: ignore[assignment]
        self.assertTrue(result["ok"])
        self.assertTrue(result["screen_diff"]["changed"])
        self.assertEqual(result["screen_diff"]["bbox_px"], {"x": 0, "y": 0, "width": 1, "height": 1})

    def test_measure_diff_never_touches_a_failed_action(self) -> None:
        inst = SimpleNamespace(
            capture=self.capture,
            gate=SimpleNamespace(ready=False),
            files=None,
        )
        failed = {"ok": False, "error": {"code": "hid_lease_required"}}
        self.assertEqual(measure_screen_diff(inst, failed, settle_ms=0), failed)
        self.assertNotIn("screen_diff", failed)

    def test_measure_diff_reports_a_failed_capture_beside_the_action(self) -> None:
        self.seed_frame()
        inst = SimpleNamespace(
            capture=self.capture,
            gate=SimpleNamespace(ready=False),
            files=None,
        )
        original = subprocess.run

        def boom(*args, **kwargs):
            raise OSError("no capture device")

        subprocess.run = boom  # type: ignore[assignment]
        try:
            result = measure_screen_diff(inst, {"ok": True}, settle_ms=0)
        finally:
            subprocess.run = original  # type: ignore[assignment]
        self.assertTrue(result["ok"])
        self.assertFalse(result["screen_diff"]["ok"])
        self.assertEqual(result["screen_diff"]["error"]["code"], "screen_capture_failed")


class RegistryWiringTests(VampirizeTestCase):
    def fake_factory(self, config):
        link = RecordingLink()
        return TitellaInstance(config, link=link, firmware=FirmwareInfo(link))

    def _config_with(self, **environment: str):
        # load_config builds the per-instance Config objects itself, so the
        # environment is the only channel that reaches config.titellas.
        return load_config(
            self.root / "absent.toml",
            {"TITELLA_STATE_DIR": str(self.root / "state2"), **environment},
        )

    def test_instance_summary_redacts_the_ocr_key(self) -> None:
        config = self._config_with(
            TITELLA_OCR_ENDPOINT="http://127.0.0.1:9/v1",
            TITELLA_OCR_MODEL="local-ocr",
            TITELLA_OCR_API_KEY="sekrit-value",
        )
        registry = TitellaRegistry(config, instance_factory=self.fake_factory)
        instance = registry.resolve(None)
        summary = instance.summary(is_default=True)
        self.assertTrue(summary["ocr_api_key_configured"])
        self.assertEqual(summary["ocr_endpoint"], "http://127.0.0.1:9/v1")
        self.assertEqual(summary["ocr_model"], "local-ocr")
        self.assertTrue(summary["screen_autocrop"])
        self.assertNotIn("sekrit-value", json.dumps(summary))
        self.assertNotIn("ocr_api_key", summary)

    def test_hid_controller_follows_the_configured_layout(self) -> None:
        config = self._config_with(TITELLA_KEYBOARD_LAYOUT="en_US")
        registry = TitellaRegistry(config, instance_factory=self.fake_factory)
        instance = registry.resolve(None)
        self.assertEqual(instance.hid.layout, "en_US")
        self.assertEqual(
            instance.hid.type_text("ops", "ñ")["error"]["code"],
            "untypeable_characters",
        )


# ── 4. configurable LLM OCR ─────────────────────────────────────────────
class OcrEndpoint:
    """Local OpenAI-compatible endpoint that records what it receives."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.status = 200
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                endpoint.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "body": body,
                    }
                )
                if endpoint.status != 200:
                    self.send_response(endpoint.status)
                    self.end_headers()
                    self.wfile.write(b'{"error": "boom"}')
                    return
                if endpoint.responses:
                    payload = endpoint.responses.pop(0)
                else:
                    payload = {
                        "choices": [{"message": {"content": "transcribed text"}}]
                    }
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class OcrTests(VampirizeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.endpoint = OcrEndpoint()
        self.addCleanup(self.endpoint.close)
        self.ocr_config = replace(
            self.config,
            ocr_endpoint=self.endpoint.base_url,
            ocr_model="local-ocr",
            ocr_api_key="secret-token",
            ocr_timeout_s=10,
        )

    def test_not_configured_is_reported_without_a_request(self) -> None:
        self.seed_frame()
        result = run_ocr(self.config, self.capture, scope="full")
        self.assertEqual(result["error"]["code"], "ocr_not_configured")
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(self.endpoint.requests, [])

    def test_screen_required_before_any_capture(self) -> None:
        result = run_ocr(self.ocr_config, self.capture, scope="full")
        self.assertEqual(result["error"]["code"], "screen_required")

    def test_full_scope_posts_an_openai_compatible_payload(self) -> None:
        image = self.seed_frame(32, 24)
        self.endpoint.responses.append(
            {"choices": [{"message": {"content": [{"type": "text", "text": "hola"}]}}]}
        )
        result = run_ocr(self.ocr_config, self.capture, scope="full", prompt="read it")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["text"], "hola")
        self.assertEqual(result["scope"], "full")
        self.assertIsNone(result["bbox_px"])
        self.assertEqual(result["model"], "local-ocr")

        request = self.endpoint.requests[-1]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["authorization"], "Bearer secret-token")
        self.assertEqual(request["content_type"], "application/json")
        body = request["body"]
        self.assertEqual(body["model"], "local-ocr")
        self.assertEqual(body["temperature"], 0)
        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "read it"})
        url = content[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), image)

    def test_default_prompt_is_used_when_none_is_given(self) -> None:
        self.seed_frame()
        result = run_ocr(self.ocr_config, self.capture, scope="full")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["text"], "transcribed text")
        content = self.endpoint.requests[-1]["body"]["messages"][0]["content"]
        self.assertEqual(content[0]["text"], DEFAULT_OCR_PROMPT)

    def test_diff_scope_sends_the_padded_changed_region(self) -> None:
        self.capture.record(encode_png_rgb(80, 60, bytes(80 * 60 * 3)), source="uvc")
        changed = bytearray(80 * 60 * 3)
        for y in range(20, 26):
            for x in range(30, 40):
                offset = (y * 80 + x) * 3
                changed[offset:offset + 3] = b"\xff\x00\x00"
        self.capture.record(encode_png_rgb(80, 60, bytes(changed)), source="uvc")

        result = run_ocr(self.ocr_config, self.capture, scope="diff")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["scope"], "diff")
        expected = {"x": 14, "y": 4, "width": 42, "height": 38}
        self.assertEqual(result["bbox_px"], expected)
        url = self.endpoint.requests[-1]["body"]["messages"][0]["content"][1]["image_url"]["url"]
        sent = base64.b64decode(url.split(",", 1)[1])
        self.assertEqual(png_resolution(sent), (42, 38))

    def test_diff_scope_without_a_change_skips_the_model(self) -> None:
        image = encode_png_rgb(40, 30, bytes(40 * 30 * 3))
        self.capture.record(image, source="uvc")
        self.capture.record(image, source="uvc")
        result = run_ocr(self.ocr_config, self.capture, scope="diff")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["changed"])
        self.assertEqual(result["text"], "")
        self.assertEqual(self.endpoint.requests, [])

    def test_diff_scope_falls_back_to_full_without_a_baseline(self) -> None:
        self.seed_frame(24, 18)
        result = run_ocr(self.ocr_config, self.capture, scope="diff")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["scope"], "full")
        self.assertIsNone(result["bbox_px"])
        self.assertEqual(len(self.endpoint.requests), 1)

    def test_bbox_scope_crops_the_requested_rectangle(self) -> None:
        self.seed_frame(40, 30)
        result = run_ocr(
            self.ocr_config,
            self.capture,
            scope="bbox",
            bbox={"x": 4, "y": 5, "width": 10, "height": 8},
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["bbox_px"], {"x": 4, "y": 5, "width": 10, "height": 8})
        url = self.endpoint.requests[-1]["body"]["messages"][0]["content"][1]["image_url"]["url"]
        self.assertEqual(png_resolution(base64.b64decode(url.split(",", 1)[1])), (10, 8))

        out_of_bounds = run_ocr(
            self.ocr_config,
            self.capture,
            scope="bbox",
            bbox={"x": 35, "y": 5, "width": 10, "height": 8},
        )
        self.assertEqual(out_of_bounds["error"]["code"], "coordinates_out_of_bounds")
        malformed = run_ocr(self.ocr_config, self.capture, scope="bbox", bbox={"x": 1})
        self.assertEqual(malformed["error"]["code"], "invalid_argument")

    def test_endpoint_with_the_full_path_is_not_doubled(self) -> None:
        self.seed_frame()
        config = replace(
            self.ocr_config,
            ocr_endpoint=self.endpoint.base_url + "/chat/completions",
        )
        result = run_ocr(config, self.capture, scope="full")
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.endpoint.requests[-1]["path"], "/v1/chat/completions"
        )

    def test_http_failure_is_a_retryable_ocr_error(self) -> None:
        self.seed_frame()
        self.endpoint.status = 500
        result = run_ocr(self.ocr_config, self.capture, scope="full")
        self.assertEqual(result["error"]["code"], "ocr_request_failed")
        self.assertTrue(result["error"]["retryable"])

    def test_unusable_response_is_reported(self) -> None:
        self.seed_frame()
        self.endpoint.responses.append({"unexpected": True})
        result = run_ocr(self.ocr_config, self.capture, scope="full")
        self.assertEqual(result["error"]["code"], "ocr_request_failed")

    def test_no_api_key_means_no_authorization_header(self) -> None:
        self.seed_frame()
        config = replace(self.ocr_config, ocr_api_key=None)
        result = run_ocr(config, self.capture, scope="full")
        self.assertTrue(result["ok"], result)
        self.assertIsNone(self.endpoint.requests[-1]["authorization"])


class ConfigSurfaceTests(unittest.TestCase):
    def test_autocrop_and_ocr_fields_load_from_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(
                Path(temp) / "absent.toml",
                {
                    "TITELLA_STATE_DIR": temp,
                    "TITELLA_SCREEN_AUTOCROP": "false",
                    "TITELLA_OCR_ENDPOINT": "http://127.0.0.1:8080/v1",
                    "TITELLA_OCR_API_KEY": "token",
                    "TITELLA_OCR_MODEL": "vision",
                    "TITELLA_OCR_TIMEOUT_S": "12.5",
                },
            )
        self.assertFalse(config.screen_autocrop)
        self.assertEqual(config.ocr_endpoint, "http://127.0.0.1:8080/v1")
        self.assertEqual(config.ocr_api_key, "token")
        self.assertEqual(config.ocr_model, "vision")
        self.assertEqual(config.ocr_timeout_s, 12.5)

    def test_ocr_defaults_stay_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(Path(temp) / "absent.toml", {"TITELLA_STATE_DIR": temp})
        self.assertTrue(config.screen_autocrop)
        self.assertIsNone(config.ocr_endpoint)
        self.assertIsNone(config.ocr_model)
        self.assertIsNone(config.ocr_api_key)
        self.assertEqual(config.ocr_timeout_s, 30.0)

    def test_instance_tables_carry_their_own_ocr_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(
                f'state_dir = "{root / "state"}"\n'
                'ocr_endpoint = "http://127.0.0.1:7000/v1"\n\n'
                '[titella.alpha]\nserial_port = "/dev/cu.alpha"\n\n'
                '[titella.beta]\nserial_port = "/dev/cu.beta"\n'
                'ocr_endpoint = "http://127.0.0.1:7001/v1"\n'
                'ocr_model = "beta-ocr"\n'
                'screen_autocrop = false\n',
                encoding="utf-8",
            )
            config = load_config(path, {})
        alpha = config.titellas["alpha"]
        beta = config.titellas["beta"]
        self.assertEqual(alpha.ocr_endpoint, "http://127.0.0.1:7000/v1")
        self.assertIsNone(alpha.ocr_model)
        self.assertTrue(alpha.screen_autocrop)
        self.assertEqual(beta.ocr_endpoint, "http://127.0.0.1:7001/v1")
        self.assertEqual(beta.ocr_model, "beta-ocr")
        self.assertFalse(beta.screen_autocrop)


if __name__ == "__main__":
    unittest.main()
