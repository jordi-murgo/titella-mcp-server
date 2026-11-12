"""Regression tests for the agent-side screen capture (fs.screen).

The receiver is exercised on a deterministic fake link: a valid multi-block
PNG completes only after the finished event and its byte counts, every
inconsistency is an error that leaves the previous frame and calibration
untouched, the size bound is enforced while collecting, a bounded wait aborts
on link loss and an unsupported agent never falls back to UVC.
"""
from __future__ import annotations

import json
import os
import struct
import tempfile
import threading
import time
import unittest
import zlib
from dataclasses import replace
from pathlib import Path

from titella.agent_state import AgentGate
from titella.config import load_config
from titella.files_client import (
    SCREEN_LIMIT_ERROR,
    SCREEN_MAX_PAYLOAD_BYTES,
    FilesClient,
    StreamCollector,
)
from titella.screen import ScreenCapture, png_resolution
from titella.serial_link import TransportError
from titella_slave_assets.wire import base_frame, data_b64


LINK = "0123456789abcdef0123456789abcdef"


def make_png(width: int, height: int, level: int = 6) -> bytes:
    """A real, decodable PNG the receiver can validate without a header fake."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for row in range(height):
        raw.append(0)
        for column in range(width):
            raw.extend(((column * 7) % 256, (row * 11) % 256, (column + row) % 256))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), level))
        + chunk(b"IEND", b"")
    )


class QueueOnlyLink:
    """SerialLink stand-in: records requests, never opens a transport."""

    def __init__(self) -> None:
        self.link_id = LINK
        self.port = None
        self.frames: list[dict] = []
        self.requests: list[dict] = []
        self.handlers: dict[tuple[str, str | None], list] = {}
        self.responder = None
        self._lock = threading.Lock()

    # -- SerialLink surface used by FilesClient ------------------------------
    def subscribe(self, channel: str, handler, session_id: str | None = None):
        self.handlers.setdefault((channel, session_id), []).append(handler)
        return lambda: self.handlers[(channel, session_id)].remove(handler)

    def send_frame(self, frame: dict) -> None:
        with self._lock:
            self.frames.append(dict(frame))

    def request(self, channel: str, op: str, body: dict | None = None, timeout: float = 6.0):
        request = {"channel": channel, "op": op, "body": dict(body or {}), "timeout": timeout}
        with self._lock:
            self.requests.append(request)
        if self.responder is None:
            raise AssertionError("the test did not script a response")
        return self.responder(channel, op, dict(body or {}))

    def dispatch(self, frame: dict) -> None:
        """Feeds a frame through the FilesClient handlers, as the link would.

        A data frame accepted by a handler is acknowledged, exactly like
        SerialLink._dispatch; a refused one produces no ACK.
        """
        session_id = frame.get("session_id")
        accepted = True
        for handler in list(self.handlers.get((frame["channel"], session_id), [])) + list(
            self.handlers.get((frame["channel"], None), [])
        ):
            if handler(frame) is False:
                accepted = False
                break
        if accepted and frame["type"] == "data":
            self.send_frame(base_frame(
                LINK, frame["channel"], "ack",
                session_id=session_id, ack_seq=frame["seq"],
            ))

    def data_frames(self, session_id: str) -> list[dict]:
        with self._lock:
            return [
                frame for frame in self.frames
                if frame.get("type") == "ack" and frame.get("session_id") == session_id
            ]


class AgentScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = replace(
            load_config(
                self.root / "absent.toml",
                {"TITELLA_STATE_DIR": str(self.root / "state")},
            ),
            screen_device="USB3 Video",
        )
        self.state = self.config.ensure_state_dir()
        self.link = QueueOnlyLink()
        self.gate = AgentGate()
        self.gate.set_ready(True)
        self.files = FilesClient(self.link, self.gate)  # type: ignore[arg-type]
        self.addCleanup(self.files.close)
        self.capture = ScreenCapture(self.config)

    # -- helpers -------------------------------------------------------------
    def stream(
        self,
        image: bytes,
        *,
        metadata: dict | None = None,
        finished_bytes: int | None = None,
        final_event: bool = True,
        chunk: int = 512,
        order: list[int] | None = None,
    ) -> None:
        """Scripts metadata + data + finished event, as the agent would send."""

        def responder(channel: str, op: str, body: dict) -> dict:
            self.assertIn("stream_id", body)
            session_id = body["stream_id"]
            resolution = png_resolution(image) or (1, 1)
            result = {
                "format": "png",
                "width_px": resolution[0],
                "height_px": resolution[1],
                "bytes": len(image),
                "source": "agent",
                "monitor": "primary",
                "stream_id": session_id,
                "payload_kind": "bytes",
            }
            result.update(metadata or {})
            blocks = [
                image[offset:offset + chunk] for offset in range(0, len(image), chunk)
            ]
            sequence = order if order is not None else list(range(len(blocks)))

            def pump() -> None:
                for seq in sequence:
                    self.link.dispatch(base_frame(
                        LINK, "fs", "data", session_id=session_id, seq=seq,
                        data_b64=data_b64(blocks[seq]),
                    ))
                if final_event:
                    self.link.dispatch(base_frame(
                        LINK, "fs", "event", session_id=session_id,
                        op="fs.stream.finished",
                        body={
                            "status": "finished",
                            "bytes": len(image) if finished_bytes is None else finished_bytes,
                            "window": 8,
                        },
                    ))

            threading.Thread(target=pump, daemon=True).start()
            return {"body": result}

        self.link.responder = responder

    def failing(self, code: str, message: str = "capture broke", retryable: bool = True) -> None:
        def responder(channel: str, op: str, body: dict) -> dict:
            raise TransportError(code, message, retryable)

        self.link.responder = responder

    def seed_previous_frame(self) -> tuple[bytes, tuple[int, int]]:
        image = make_png(16, 9)
        metadata, returned = self.capture.record(image, source="USB3 Video")
        self.assertTrue(metadata["ok"], metadata)
        self.assertEqual(returned, image)
        return image, (16, 9)

    # -- happy path ----------------------------------------------------------
    def test_multi_block_png_completes_and_updates_the_calibration(self) -> None:
        image = make_png(80, 45)
        self.assertGreater(len(image), 4 * 512, "the fixture must span several blocks")
        self.stream(image, metadata={"backend": "wayland-portal"})

        metadata, returned = self.capture.agent(self.files)

        self.assertTrue(metadata["ok"], metadata)
        self.assertEqual(metadata["source"], "agent")
        self.assertEqual(metadata["monitor"], "primary")
        self.assertEqual(metadata["backend"], "wayland-portal")
        self.assertEqual(
            metadata["capture_resolution"], {"width_px": 80, "height_px": 45},
        )
        self.assertEqual(returned, image)
        self.assertEqual(self.capture.resolution(), (80, 45))
        self.assertEqual(self.capture.last_frame_path.read_bytes(), image)
        self.assertEqual(self.link.requests[0]["op"], "screen")
        self.assertEqual(self.link.requests[0]["channel"], "fs")

    def test_out_of_order_windowed_blocks_reassemble_in_sequence(self) -> None:
        image = make_png(90, 50)
        blocks = (len(image) + 511) // 512
        self.assertGreater(blocks, 3, "the fixture must span several blocks")
        # A windowed sender emits interleaved and repeated blocks.
        order = [2, 0, 1, 1, 0, *range(3, blocks)]
        self.stream(image, order=order)

        metadata, returned = self.capture.agent(self.files)

        self.assertTrue(metadata["ok"], metadata)
        self.assertEqual(returned, image)
        self.assertEqual(self.capture.resolution(), (90, 50))

    def test_out_of_order_blocks_are_all_acknowledged_and_held(self) -> None:
        """Refusing costs a resend storm that truncates frames on this link."""
        collector = StreamCollector(ordered=True)
        self.assertTrue(collector.accept(0, b"a" * 512))
        # A block past a hole is acknowledged and held, not refused.
        self.assertTrue(collector.accept(2, b"c" * 512))
        self.assertTrue(collector.accept(4, b"e" * 512))
        self.assertEqual(bytes(collector.payload), b"a" * 512)
        self.assertIsNone(collector.error)
        # A repeat is acknowledged without being applied twice.
        self.assertTrue(collector.accept(2, b"c" * 512))
        self.assertEqual(len(collector.pending), 2)
        # The holes arrive and everything held drains in order.
        self.assertTrue(collector.accept(1, b"b" * 512))
        self.assertTrue(collector.accept(3, b"d" * 512))
        self.assertEqual(
            bytes(collector.payload),
            b"".join(block * 512 for block in (b"a", b"b", b"c", b"d", b"e")),
        )
        self.assertEqual(collector.next_seq, 5)
        self.assertEqual(collector.pending, {})

    def test_hold_is_bounded_by_the_payload_cap(self) -> None:
        collector = StreamCollector(ordered=True, limit=1024, limit_error=("screen_too_large", "too big"))
        self.assertTrue(collector.accept(0, b"a" * 512))
        self.assertTrue(collector.accept(2, b"c" * 512))
        # The hole is never filled: the cap is what stops the hold growing.
        self.assertFalse(collector.accept(1, b"b" * 1024))
        self.assertEqual(collector.error["code"], "screen_too_large")
        self.assertTrue(collector.done.is_set())

    def test_lost_block_is_recovered_on_resend(self) -> None:
        image = make_png(60, 40)
        blocks = (len(image) + 511) // 512
        self.assertGreater(blocks, 3)
        # Sequence 1 is lost, the rest arrives: the master refuses everything
        # past the bounded look-ahead rather than acknowledging a hole, and the
        # sender's own retry delivers 1 again. The stream then completes in
        # order and the assembled PNG is byte-identical to the original.
        order = [0, *range(2, blocks), 1, *range(2, blocks)]
        self.stream(image, order=order)

        metadata, returned = self.capture.agent(self.files)

        self.assertTrue(metadata["ok"], metadata)
        self.assertEqual(returned, image)
        self.assertEqual(self.capture.resolution(), (60, 40))

    def test_unrecovered_gap_never_yields_a_partial_image(self) -> None:
        image = make_png(60, 40)
        blocks = (len(image) + 511) // 512
        self.assertGreater(blocks, 2)
        previous, resolution = self.seed_previous_frame()
        # Sequence 1 never arrives at all: no later block may be assembled into
        # a complete image, so the caller gets a timeout and no image.
        self.stream(image, order=[0, *range(2, blocks)], final_event=False)
        with self.assertRaises(TransportError) as context:
            self.files._request(
                "screen", stream_timeout=0.3, max_payload_bytes=SCREEN_MAX_PAYLOAD_BYTES,
                limit_error=SCREEN_LIMIT_ERROR, ordered=True,
            )
        self.assertEqual(context.exception.code, "stream_timeout")
        self.assertEqual(self.capture.last_frame_path.read_bytes(), previous)
        self.assertEqual(self.capture.resolution(), resolution)

    def test_agent_capture_calibration_drives_mouse_abs_mapping(self) -> None:
        from titella.hid import HidController, HidLease

        image = make_png(1280, 720)
        self.stream(image)
        metadata, _ = self.capture.agent(self.files)
        self.assertTrue(metadata["ok"], metadata)

        hid_requests: list[tuple[str, dict]] = []
        self.link.request = lambda channel, op, body=None, timeout=6.0: (  # type: ignore[assignment]
            hid_requests.append((op, dict(body or {}))) or {"body": {"ok": True}}
        )
        lease = HidLease(self.config)
        lease.acquire("tester", 600)
        controller = HidController(self.link, lease, self.capture)  # type: ignore[arg-type]

        result = controller.mouse_abs("tester", 640, 360)
        self.assertTrue(result["ok"], result)
        op, body = hid_requests[-1]
        self.assertEqual(op, "mouse_abs")
        self.assertEqual(body["x"], round(640 * 32767 / 1279))
        self.assertEqual(body["y"], round(360 * 32767 / 719))
        self.assertEqual(
            result["coords"]["capture_res"], [1280, 720],
            "the agent capture, not a stale UVC resolution, calibrates the cursor",
        )

    # -- failures leave the previous capture intact --------------------------
    def test_transport_failure_keeps_the_previous_frame_and_calibration(self) -> None:
        previous, resolution = self.seed_previous_frame()
        self.failing("agent_not_ready", "the slave agent is not running; run slave_bootstrap")

        metadata, returned = self.capture.agent(self.files)

        self.assertIsNone(returned, "a failed capture never returns an image")
        self.assertFalse(metadata["ok"])
        self.assertEqual(metadata["error"]["code"], "agent_not_ready")
        self.assertEqual(self.capture.last_frame_path.read_bytes(), previous)
        self.assertEqual(self.capture.resolution(), resolution)

    def test_missing_final_event_times_out_without_touching_the_cache(self) -> None:
        previous, resolution = self.seed_previous_frame()
        image = make_png(24, 12)
        self.stream(image, final_event=False)

        original = replace(self.config, screen_device="USB3 Video")
        with self.assertRaises(TransportError) as context:
            self.files._request(
                "screen",
                stream_timeout=0.3,
                max_payload_bytes=SCREEN_MAX_PAYLOAD_BYTES,
            )
        self.assertEqual(context.exception.code, "stream_timeout")
        self.assertEqual(self.capture.last_frame_path.read_bytes(), previous)
        self.assertEqual(self.capture.resolution(), resolution)
        self.assertEqual(original.screen_device, "USB3 Video")

    def test_final_event_error_status_is_reported_and_cache_untouched(self) -> None:
        previous, resolution = self.seed_previous_frame()
        image = make_png(24, 12)

        def responder(channel: str, op: str, body: dict) -> dict:
            session_id = body["stream_id"]

            def pump() -> None:
                self.link.dispatch(base_frame(
                    LINK, "fs", "data", session_id=session_id, seq=0,
                    data_b64=data_b64(image[:512]),
                ))
                self.link.dispatch(base_frame(
                    LINK, "fs", "event", session_id=session_id,
                    op="fs.stream.finished",
                    body={"status": "error", "code": "ack_timeout", "message": "no ack", "retryable": True},
                ))

            threading.Thread(target=pump, daemon=True).start()
            return {"body": {
                "format": "png", "width_px": 24, "height_px": 12, "bytes": len(image),
                "source": "agent", "monitor": "primary", "stream_id": session_id,
                "payload_kind": "bytes",
            }}

        self.link.responder = responder
        metadata, returned = self.capture.agent(self.files)
        self.assertIsNone(returned)
        self.assertEqual(metadata["error"]["code"], "ack_timeout")
        self.assertEqual(self.capture.last_frame_path.read_bytes(), previous)
        self.assertEqual(self.capture.resolution(), resolution)

    def test_truncated_transfer_is_a_retryable_error_without_an_image(self) -> None:
        """The link lost bytes: never publish a partial image, and allow retry."""
        previous, resolution = self.seed_previous_frame()
        image = make_png(20, 10)
        self.stream(image, finished_bytes=len(image) - 1)

        metadata, returned = self.capture.agent(self.files)

        self.assertIsNone(returned)
        self.assertEqual(metadata["error"]["code"], "screen_capture_failed")
        self.assertTrue(metadata["error"]["retryable"])
        self.assertIn("truncated", metadata["error"]["message"])
        self.assertEqual(self.capture.last_frame_path.read_bytes(), previous)
        self.assertEqual(self.capture.resolution(), resolution)

    def test_declared_dimensions_must_match_the_png(self) -> None:
        previous, _ = self.seed_previous_frame()
        image = make_png(20, 10)
        self.stream(image, metadata={"width_px": 1920, "height_px": 1080})

        metadata, returned = self.capture.agent(self.files)

        self.assertIsNone(returned)
        self.assertEqual(metadata["error"]["code"], "invalid_screen_capture")
        self.assertEqual(self.capture.last_frame_path.read_bytes(), previous)

    def test_non_png_payload_is_rejected(self) -> None:
        previous, _ = self.seed_previous_frame()
        junk = b"not a png at all" * 40
        self.stream(junk, metadata={"width_px": 1, "height_px": 1})

        metadata, returned = self.capture.agent(self.files)

        self.assertIsNone(returned)
        self.assertEqual(metadata["error"]["code"], "invalid_screen_capture")
        self.assertEqual(self.capture.last_frame_path.read_bytes(), previous)

    def test_unsupported_agent_is_reported_without_a_uvc_fallback(self) -> None:
        previous, _ = self.seed_previous_frame()
        # The Python agent answers unknown_op for fs.screen.
        self.failing("unknown_op", "unknown fs operation", retryable=False)
        ffmpeg_calls: list[list[str]] = []
        self.capture.command = lambda: ffmpeg_calls.append(["ffmpeg"]) or ["ffmpeg"]  # type: ignore[assignment]

        metadata, returned = self.capture.agent(self.files)

        self.assertIsNone(returned)
        self.assertEqual(metadata["error"]["code"], "screen_unsupported")
        self.assertFalse(metadata["error"]["retryable"])
        self.assertEqual(ffmpeg_calls, [], "the UVC path is never a substitute")
        self.assertEqual(self.capture.last_frame_path.read_bytes(), previous)

    def test_not_ready_gate_fails_immediately_without_any_request(self) -> None:
        previous, _ = self.seed_previous_frame()
        self.gate.set_ready(False)
        self.link.responder = lambda *args: self.fail("no request may leave a closed gate")

        metadata, returned = self.capture.agent(self.files)

        self.assertIsNone(returned)
        self.assertEqual(metadata["error"]["code"], "agent_not_ready")
        self.assertEqual(self.link.requests, [])
        self.assertEqual(self.capture.last_frame_path.read_bytes(), previous)

    # -- bounded receiver ----------------------------------------------------
    def test_size_bound_is_enforced_while_collecting(self) -> None:
        collector = StreamCollector(limit=700, limit_error=("screen_too_large", "too big"))
        files = self.files
        files._streams["bounded"] = collector

        def frame(seq: int, payload: bytes) -> dict:
            return base_frame(
                LINK, "fs", "data", session_id="bounded", seq=seq,
                data_b64=data_b64(payload),
            )

        self.assertTrue(files._on_fs(frame(0, b"a" * 512)))
        self.assertEqual(len(collector.payload), 512)
        # A second 512-byte block would reach 1024, past the 700-byte bound:
        # it is rejected, failed and not acknowledged.
        self.assertFalse(files._on_fs(frame(1, b"b" * 512)))
        self.assertEqual(collector.error["code"], "screen_too_large")
        self.assertTrue(collector.done.is_set())
        self.assertEqual(len(collector.payload), 512, "the overflow is not appended")
        # Further data of the abandoned stream is neither accepted nor acked.
        self.assertFalse(files._on_fs(frame(2, b"c" * 10)))
        self.assertEqual(len(collector.payload), 512)

    def test_first_error_survives_a_later_completion_event(self) -> None:
        collector = StreamCollector(limit=8, limit_error=("screen_too_large", "too big"))
        self.files._streams["bounded"] = collector
        self.assertFalse(self.files._on_fs(base_frame(
            LINK, "fs", "data", session_id="bounded", seq=0, data_b64=data_b64(b"x" * 9),
        )))
        self.files._on_fs(base_frame(
            LINK, "fs", "event", session_id="bounded", op="fs.stream.finished",
            body={"status": "finished", "bytes": 9},
        ))
        self.assertEqual(collector.error["code"], "screen_too_large")

    def test_oversized_stream_raises_screen_too_large(self) -> None:
        image = make_png(64, 64)
        self.stream(image)
        with self.assertRaises(TransportError) as context:
            self.files._request(
                "screen",
                stream_timeout=5.0,
                max_payload_bytes=1024,
                limit_error=("screen_too_large", "the image exceeds the bound"),
            )
        self.assertEqual(context.exception.code, "screen_too_large")
        self.assertFalse(context.exception.retryable)

    def test_bounded_wait_aborts_when_the_agent_disappears(self) -> None:
        image = make_png(32, 16)

        def responder(channel: str, op: str, body: dict) -> dict:
            session_id = body["stream_id"]
            self.link.dispatch(base_frame(
                LINK, "fs", "data", session_id=session_id, seq=0,
                data_b64=data_b64(image[:512]),
            ))
            # The stream never finishes and the link drops the agent.
            threading.Timer(0.2, lambda: self.gate.set_ready(False)).start()
            return {"body": {
                "format": "png", "width_px": 32, "height_px": 16, "bytes": len(image),
                "source": "agent", "monitor": "primary", "stream_id": session_id,
                "payload_kind": "bytes",
            }}

        self.link.responder = responder
        started = time.monotonic()
        with self.assertRaises(TransportError) as context:
            self.files._request(
                "screen", stream_timeout=60.0, max_payload_bytes=SCREEN_MAX_PAYLOAD_BYTES,
            )
        elapsed = time.monotonic() - started
        self.assertEqual(context.exception.code, "agent_not_ready")
        self.assertLess(elapsed, 10, "a known link loss must not wait the full deadline")

    def test_unregistered_stream_stops_acknowledging_after_an_abandon(self) -> None:
        """An abandon unregisters the collector; later blocks are unacknowledged."""
        image = make_png(40, 20)
        self.stream(image, final_event=False)
        with self.assertRaises(TransportError) as context:
            self.files._request(
                "screen",
                stream_timeout=0.15,
                max_payload_bytes=SCREEN_MAX_PAYLOAD_BYTES,
            )
        self.assertEqual(context.exception.code, "stream_timeout")
        session_id = self.link.requests[0]["body"]["stream_id"]
        self.assertNotIn(session_id, self.files._streams)
        accepted_before = len(self.link.data_frames(session_id))
        self.assertGreater(accepted_before, 0, "the accepted blocks were acknowledged")
        # A late block of the abandoned stream is refused: no ACK is emitted,
        # so the slave sender stops after its bounded retries.
        accepted = self.files._on_fs(base_frame(
            LINK, "fs", "data", session_id=session_id, seq=99, data_b64=data_b64(b"z" * 512),
        ))
        self.assertFalse(accepted)
        self.assertEqual(len(self.link.data_frames(session_id)), accepted_before)

    def test_uvc_path_keeps_its_contract(self) -> None:
        image = make_png(32, 18)

        class Completed:
            returncode = 0
            stdout = image
            stderr = b""

        import subprocess

        original = subprocess.run
        subprocess.run = lambda *args, **kwargs: Completed()  # type: ignore[assignment]
        try:
            metadata, returned = self.capture.capture()
        finally:
            subprocess.run = original  # type: ignore[assignment]
        self.assertTrue(metadata["ok"], metadata)
        self.assertEqual(metadata["source"], "USB3 Video")
        self.assertNotIn("monitor", metadata)
        self.assertEqual(returned, image)
        self.assertEqual(self.capture.resolution(), (32, 18))


if __name__ == "__main__":
    unittest.main()
