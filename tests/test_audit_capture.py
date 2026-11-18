"""Capture, OCR-provenance and transfer-evidence tests under audit mode."""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from titella.agent_state import AgentGate
from titella.audit import AuditManager
from titella.config import load_config
from titella.files_client import FilesClient
from titella.ocr import run_ocr
from titella.png_codec import crop_rgb, encode_png_rgb
from titella.screen import ScreenCapture, png_resolution

from tests.test_screen_agent import make_png


class _FakeOCRHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(  # type: ignore[attr-defined]
            {"path": self.path, "body": json.loads(body),
             "authorization": self.headers.get("Authorization")}
        )
        # The server object doubles as the test's barrier: got_request fires
        # when the model call is in flight, and an optional hold blocks the
        # response so a concurrent capture can be interleaved deterministically.
        self.server.got_request.set()  # type: ignore[attr-defined]
        hold = self.server.hold  # type: ignore[attr-defined]
        if hold is not None:
            hold.wait(timeout=10)
        payload = {
            "choices": [{"message": {"content": [{"type": "text", "text": "texto"}]}}]
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        return


class _OCRServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.got_request = threading.Event()
        self.hold: threading.Event | None = None
        super().__init__(("127.0.0.1", 0), _FakeOCRHandler)

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/v1"


class CaptureAuditTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audit_root = self.root / "audits"
        self.manager = AuditManager(self.audit_root)
        self.addCleanup(self.manager.close)
        self.manager.start()
        self.config = replace(
            load_config(
                self.root / "absent.toml",
                {"TITELLA_STATE_DIR": str(self.root / "state")},
            ),
            screen_device="Fake card",
        )
        self.capture = ScreenCapture(self.config)
        from titella.audit import _CaptureEvidenceSink

        self.sink = _CaptureEvidenceSink(self.manager, "lab")
        self.capture.evidence_sink = self.sink
        self.activated = self.manager.activate("caps", "lab")

    def _artifacts(self) -> Path:
        return self.audit_root / "caps" / "artifacts"


class CaptureTests(CaptureAuditTestCase):
    def test_two_pngs_survive_cache_replacement_with_own_ids(self) -> None:
        first, second = make_png(40, 30), make_png(50, 40)
        meta1, _ = self.capture.record(first, source="uvc")
        meta2, _ = self.capture.record(second, source="uvc")
        self.manager.flush_barrier()
        pngs = sorted(self._artifacts().glob("*.png"))
        self.assertEqual(len(pngs), 2)
        stored = {p.read_bytes() for p in pngs}
        self.assertEqual(stored, {first, second})
        ev1 = self.capture._last_evidence
        self.assertEqual(ev1["frame_sha256"], hashlib.sha256(second).hexdigest())

    def test_identical_pngs_get_distinct_evidence_ids(self) -> None:
        frame = make_png(30, 20)
        self.capture.record(frame, source="uvc")
        first_id = self.capture._last_evidence["evidence_id"]
        self.capture.record(frame, source="uvc")
        second_id = self.capture._last_evidence["evidence_id"]
        self.manager.flush_barrier()
        self.assertNotEqual(first_id, second_id)
        pngs = list(self._artifacts().glob("*.png"))
        self.assertEqual(len(pngs), 2)
        self.assertTrue(all(p.read_bytes() == frame for p in pngs))

    def test_invalid_png_is_a_failure_without_evidence(self) -> None:
        self.capture.record(make_png(30, 20), source="uvc")
        good_evidence = self.capture._last_evidence
        before = sorted(self._artifacts().glob("*.png"))
        meta, image = self.capture.record(b"definitely not a png", source="uvc")
        self.assertFalse(meta["ok"])
        self.assertEqual(meta["error"]["code"], "invalid_screen_capture")
        self.assertIsNone(image)
        self.manager.flush_barrier()
        self.assertEqual(sorted(self._artifacts().glob("*.png")), before)
        # The invalid bytes never replace the provenance of the valid frame.
        self.assertEqual(self.capture._last_evidence, good_evidence)

    def test_corrupt_idat_with_valid_ihdr_is_not_evidence(self) -> None:
        self.capture.record(make_png(30, 20), source="uvc")
        self.manager.flush_barrier()
        good_evidence = self.capture._last_evidence
        good_cache = self.capture.last_frame_path.read_bytes()
        before = sorted(self._artifacts().glob("*.png"))
        events_before = (self.audit_root / "caps" / "events.jsonl").read_text()

        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + b"\x00\x00\x00\x00"  # decode_png does not verify the CRC
            )

        # A valid IHDR that advertises a plausible image, with unusable IDAT:
        # the header alone would pass png_resolution().
        malformed = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 30, 20, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", b"not a zlib stream")
            + chunk(b"IEND", b"")
        )
        self.assertEqual(png_resolution(malformed), (30, 20))

        meta, image = self.capture.record(malformed, source="uvc")
        self.assertFalse(meta["ok"])
        self.assertEqual(meta["error"]["code"], "invalid_screen_capture")
        self.assertIsNone(image)
        self.manager.flush_barrier()
        # No success evidence, no cache corruption and no provenance rewrite.
        self.assertEqual(sorted(self._artifacts().glob("*.png")), before)
        self.assertEqual(self.capture.last_frame_path.read_bytes(), good_cache)
        self.assertEqual(self.capture._last_evidence, good_evidence)
        self.assertEqual(
            (self.audit_root / "caps" / "events.jsonl").read_text(), events_before
        )

    def test_cache_write_failure_archives_valid_png(self) -> None:
        frame = make_png(32, 24)
        self.config.state_dir.chmod(0o500)
        self.addCleanup(self.config.state_dir.chmod, 0o700)
        meta, image = self.capture.record(frame, source="uvc")
        self.assertFalse(meta["ok"])
        self.assertEqual(meta["error"]["code"], "screen_state_failed")
        self.assertFalse(meta["cache_saved"])
        self.assertIsNone(image)
        self.manager.flush_barrier()
        pngs = list(self._artifacts().glob("*.png"))
        self.assertEqual(len(pngs), 1)
        self.assertEqual(pngs[0].read_bytes(), frame)
        events = [
            json.loads(line)
            for line in (self.audit_root / "caps" / "events.jsonl").read_text().splitlines()
        ]
        created = [
            event["data"]
            for event in events
            if event.get("kind") == "evidence.created"
            and (event.get("data") or {}).get("role") == "capture"
        ]
        self.assertEqual(len(created), 1)
        self.assertIs(created[0]["cache_saved"], False)
        # The cache does not hold this frame, so no auditable reference may
        # claim it does.
        self.assertIsNone(self.capture._last_evidence)

    def test_deactivated_diff_keeps_current_semantics(self) -> None:
        self.manager.deactivate("lab")
        rgb = bytearray(40 * 30 * 3)
        self.capture.record(encode_png_rgb(40, 30, bytes(rgb)), source="uvc")
        rgb[300 * 3:300 * 3 + 60] = b"\xff" * 60
        self.capture.record(encode_png_rgb(40, 30, bytes(rgb)), source="uvc")
        meta, image = self.capture.screen_diff(padding_px=2)
        self.assertTrue(meta["ok"], meta)
        self.assertTrue(meta["changed"])
        self.assertIsNotNone(image)
        self.manager.flush_barrier()
        self.assertEqual(list(self._artifacts().glob("*.png")), [])
        events = (self.audit_root / "caps" / "events.jsonl").read_text()
        self.assertNotIn("capture.recorded", events)

    def test_diff_crop_is_derived_evidence_with_related_frame(self) -> None:
        rgb = bytearray(60 * 40 * 3)
        first = encode_png_rgb(60, 40, bytes(rgb))
        rgb[500 * 3:500 * 3 + 90] = b"\xff" * 90
        second = encode_png_rgb(60, 40, bytes(rgb))
        self.capture.record(first, source="uvc")
        self.capture.record(second, source="uvc")
        frame_ev = self.capture._last_evidence
        meta, image = self.capture.screen_diff(padding_px=4)
        self.manager.flush_barrier()
        self.assertTrue(meta["ok"], meta)
        events = [
            json.loads(line)
            for line in (self.audit_root / "caps" / "events.jsonl").read_text().splitlines()
        ]
        diff_events = [
            event["data"]
            for event in events
            if event.get("kind") == "evidence.created"
            and (event.get("data") or {}).get("role") == "diff"
        ]
        self.assertEqual(len(diff_events), 1)
        # The derived crop carries the full frame's hash and links back to it.
        self.assertEqual(diff_events[0]["frame_sha256"], frame_ev["frame_sha256"])
        self.assertEqual(diff_events[0]["related_evidence_ids"], [frame_ev["evidence_id"]])

    def test_stale_or_foreign_cache_rejected_for_diff(self) -> None:
        # capture produced BEFORE activation semantics: fake by clearing the
        # provenance reference while keeping the cache file
        self.capture.record(make_png(30, 20), source="uvc")
        self.capture._last_evidence = None
        meta, image = self.capture.screen_diff()
        self.assertFalse(meta["ok"])
        self.assertEqual(meta["error"]["code"], "audit_capture_required")
        self.assertIsNone(image)
        # Only the rejected cache hash/metadata is reported, never the image.
        self.assertEqual(
            meta["frame_sha256"], hashlib.sha256(self.capture.last_frame_path.read_bytes()).hexdigest()
        )
        self.manager.flush_barrier()
        self.assertEqual(list(self._artifacts().glob("*.png")).__len__(), 1)


class OCRTests(CaptureAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.endpoint = _OCRServer()
        self.addCleanup(self.endpoint.shutdown)
        threading.Thread(target=self.endpoint.serve_forever, daemon=True).start()
        self.addCleanup(
            lambda: self.endpoint.hold.set() if self.endpoint.hold is not None else None
        )
        self.ocr_config = replace(
            self.config,
            ocr_endpoint=self.endpoint.base_url,
            ocr_model="test-ocr",
            ocr_api_key="test-credential-not-evidence",
        )

    def test_ocr_bytes_hash_archived_and_text_linked(self) -> None:
        frame = make_png(48, 32)
        self.capture.record(frame, source="uvc")
        result = run_ocr(self.ocr_config, self.capture, scope="full")
        self.manager.flush_barrier()
        self.assertTrue(result["ok"], result)
        # endpoint saw exactly the frame bytes whose hash we archived
        sent_b64 = self.endpoint.requests[-1]["body"]["messages"][0]["content"][1][
            "image_url"
        ]["url"].split(",", 1)[1]
        sent_bytes = base64.b64decode(sent_b64)
        last = self.capture._last_evidence
        self.assertEqual(sent_bytes, frame)
        self.assertEqual(hashlib.sha256(sent_bytes).hexdigest(), last["frame_sha256"])
        self.assertEqual(result["source_evidence_id"], last["evidence_id"])
        self.assertEqual(result["frame_sha256"], last["frame_sha256"])
        events = (self.audit_root / "caps" / "events.jsonl").read_text()
        self.assertIn("ocr.recorded", events)
        self.assertEqual(self.endpoint.requests[-1]["authorization"], "Bearer test-credential-not-evidence")
        self.assertNotIn(self.ocr_config.ocr_api_key, events)
        for path in self._artifacts().iterdir():
            self.assertNotIn(self.ocr_config.ocr_api_key.encode(), path.read_bytes())

    def test_exact_crop_bytes_reach_endpoint_and_archive(self) -> None:
        width, height = 40, 30
        rgb = bytes((7, 8, 9)) * (width * height)
        frame = encode_png_rgb(width, height, rgb)
        self.capture.record(frame, source="uvc")
        result = run_ocr(
            self.ocr_config,
            self.capture,
            scope="bbox",
            bbox={"x": 4, "y": 5, "width": 10, "height": 8},
        )
        self.assertTrue(result["ok"], result)
        expected = encode_png_rgb(10, 8, crop_rgb(width, height, rgb, 4, 5, 10, 8))
        url = self.endpoint.requests[-1]["body"]["messages"][0]["content"][1][
            "image_url"
        ]["url"]
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), expected)
        self.manager.flush_barrier()
        events = [
            json.loads(line)
            for line in (self.audit_root / "caps" / "events.jsonl").read_text().splitlines()
        ]
        ocr_ids = [
            event["data"]["image_evidence_id"]
            for event in events
            if event.get("kind") == "ocr.recorded"
        ]
        self.assertEqual(len(ocr_ids), 1)
        # The archived OCR input is the exact PNG the model received; the
        # result is linked to the full frame's hash.
        self.assertEqual(
            (self._artifacts() / f"{ocr_ids[0]}.png").read_bytes(), expected
        )
        self.assertEqual(result["frame_sha256"], hashlib.sha256(frame).hexdigest())

    def test_concurrent_capture_does_not_rebind_ocr_provenance(self) -> None:
        first, second = make_png(36, 28), make_png(44, 33)
        self.capture.record(first, source="uvc")
        first_evidence = self.capture._last_evidence
        self.endpoint.hold = threading.Event()
        self.endpoint.got_request.clear()
        outcome: dict[str, Any] = {}

        def _run() -> None:
            outcome["result"] = run_ocr(self.ocr_config, self.capture, scope="full")

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        self.assertTrue(self.endpoint.got_request.wait(timeout=10))
        # A capture lands while the model is thinking: the OCR result must
        # still describe the frame whose bytes were sent.
        self.capture.record(second, source="uvc")
        self.endpoint.hold.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.manager.flush_barrier()
        result = outcome["result"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_evidence_id"], first_evidence["evidence_id"])
        self.assertEqual(
            result["frame_sha256"], hashlib.sha256(first).hexdigest()
        )
        self.assertEqual(
            self.capture._last_evidence["frame_sha256"],
            hashlib.sha256(second).hexdigest(),
        )
        events = [
            json.loads(line)
            for line in (self.audit_root / "caps" / "events.jsonl").read_text().splitlines()
        ]
        ocr_events = [
            event["data"]
            for event in events
            if event.get("kind") == "ocr.recorded"
        ]
        self.assertEqual(len(ocr_events), 1)
        self.assertEqual(
            ocr_events[0]["frame_sha256"], hashlib.sha256(first).hexdigest()
        )

    def test_pre_activation_cache_is_rejected_without_copying(self) -> None:
        # A frame captured while no audit was active may not feed OCR once an
        # audit opens: only its hash/metadata is reported, nothing is copied.
        self.manager.deactivate("lab")
        self.capture.record(make_png(24, 16), source="uvc")
        self.assertIsNone(self.capture._last_evidence)
        self.manager.activate("caps", "lab")
        result = run_ocr(self.ocr_config, self.capture, scope="full")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "audit_capture_required")
        self.assertEqual(
            result["frame_sha256"],
            hashlib.sha256(self.capture.last_frame_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(self.endpoint.requests, [])
        self.manager.flush_barrier()
        events = (self.audit_root / "caps" / "events.jsonl").read_text()
        self.assertNotIn("ocr.recorded", events)

    def test_reactivation_invalidates_the_previous_run_reference(self) -> None:
        self.capture.record(make_png(24, 16), source="uvc")
        self.manager.deactivate("lab")
        self.manager.activate("caps", "lab")
        result = run_ocr(self.ocr_config, self.capture, scope="full")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "audit_capture_required")
        self.assertEqual(self.endpoint.requests, [])

    def test_deactivated_ocr_keeps_current_semantics(self) -> None:
        self.manager.deactivate("lab")
        frame = make_png(24, 16)
        self.capture.record(frame, source="uvc")
        result = run_ocr(self.ocr_config, self.capture, scope="full")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["text"], "texto")
        # No audit fields, no hashing and no journal work while inactive.
        self.assertNotIn("source_evidence_id", result)
        self.assertNotIn("frame_sha256", result)
        sent_b64 = self.endpoint.requests[-1]["body"]["messages"][0]["content"][1][
            "image_url"
        ]["url"].split(",", 1)[1]
        self.assertEqual(base64.b64decode(sent_b64), frame)
        self.manager.flush_barrier()
        self.assertEqual(list(self._artifacts().glob("*.png")), [])
        events = (self.audit_root / "caps" / "events.jsonl").read_text()
        self.assertNotIn("ocr.recorded", events)


class TransferAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audit_root = self.root / "audits"
        self.manager = AuditManager(self.audit_root)
        self.addCleanup(self.manager.close)
        self.manager.start()
        self.activated = self.manager.activate("xfer", "lab")
        from tests.test_vampirize_features import RecordingLink

        self.link = RecordingLink()
        self.gate = AgentGate()
        self.gate.set_ready(True)
        self.files = FilesClient(self.link, self.gate)  # type: ignore[arg-type]
        self.files.audit_manager = self.manager
        self.files.audit_titella_id = "lab"
        self.addCleanup(self.files.close)
        from titella.audit import AuditCall, set_current_audit_call, reset_current_audit_call

        self.call = AuditCall(
            action_id="d" * 32, audit_id="xfer", run_id=self.activated["run_id"],
            titella_id="lab", tool="remote_upload", started_monotonic=0.0,
            started_utc="2026-01-01T00:00:00Z",
        )
        token = set_current_audit_call(self.call)
        self.addCleanup(lambda: reset_current_audit_call(token))

    def _artifact_bytes(self, evidence_id: str) -> bytes:
        return (self.audit_root / "xfer" / "artifacts" / f"{evidence_id}.bin").read_bytes()

    def _find_stream(self, role: str) -> str:
        self.manager.flush_barrier()
        for path in sorted((self.audit_root / "xfer" / "artifacts").glob("*.bin")):
            events = (self.audit_root / "xfer" / "events.jsonl").read_text()
        # locate by scanning events for the role
        for line in (self.audit_root / "xfer" / "events.jsonl").read_text().splitlines():
            event = json.loads(line)
            data = event.get("data") or {}
            if event.get("kind") == "evidence.created" and data.get("role") == role:
                return str(data["evidence_id"])
        raise AssertionError(f"no {role} stream recorded")

    def test_download_evidence_matches_transmitted_bytes(self) -> None:
        # deterministic 65553-byte payload through the fake transfer pump
        payload = bytes((i * 31 + 7) % 256 for i in range(65553))
        remote = self.root / "remote.bin"
        remote.write_bytes(payload)
        # script a download that streams the payload in DATA_CHUNK_BYTES blocks
        from titella_slave_assets.wire import DATA_CHUNK_BYTES

        state: dict[str, Any] = {"seq": 0}

        # RecordingLink answers every request from link.reply: script the
        # download.begin metadata before the worker consumes it.
        self.link.reply = {"body": {
            "status": "started", "bytes_total": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }}

        # download() runs on the test thread (the ContextVar with the
        # AuditCall does not inherit into worker threads); the worker then
        # streams the scripted frames we pump below.
        local = self.root / "downloaded.bin"
        result = self.files.download(
            str(remote), str(local), checksum=None, sync_timeout=0.05
        )
        self.link.reply = {"body": {"ok": True, "status": "hid_ready"}}
        transfer_id = result["transfer_id"]
        self.assertIn(transfer_id, self.files._transfers)
        from titella_slave_assets.wire import base_frame, data_b64

        seq = 0
        for offset in range(0, len(payload), DATA_CHUNK_BYTES):
            block = payload[offset:offset + DATA_CHUNK_BYTES]
            frame = base_frame(
                self.link.link_id, "transfer", "data",
                session_id=transfer_id, seq=seq,
                data_b64=data_b64(block),
            )
            self.files._on_transfer(frame)
            seq += 1
        finish = base_frame(
            self.link.link_id, "transfer", "event",
            session_id=transfer_id, op="transfer.finished",
        )
        finish["body"] = {"status": "finished", "bytes": len(payload)}
        self.files._on_transfer(finish)
        deadline = __import__("time").monotonic() + 10
        while __import__("time").monotonic() < deadline:
            status = self.files.transfer_status(transfer_id)
            if status["status"] != "running":
                break
            __import__("time").sleep(0.02)
        self.assertEqual(status["status"], "finished", status)
        self.manager.flush_barrier()
        stream = self._find_stream("download")
        archived = self._artifact_bytes(stream)
        self.assertEqual(hashlib.sha256(archived).hexdigest(),
                         hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    unittest.main()
