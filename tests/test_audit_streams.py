"""Deterministic audit-evidence tests for shell streams and transfers.

The harness mirrors ``TitellaInstance.attach_audit``: the manager's shell and
link-state subscribers are registered on the link and the clients receive
``audit_manager``/``audit_titella_id``. Frames are pumped synchronously by a
scripted link; durability comes from ``flush_barrier()`` and worker
completion from Events — no test sleeps and no polling loops.
"""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from titella.agent_state import AgentGate
from titella.audit import (
    AuditCall,
    AuditManager,
    reset_current_audit_call,
    set_current_audit_call,
)
from titella.files_client import FilesClient
from titella.serial_link import TransportError
from titella.shell_client import ShellClient
from titella_slave_assets.wire import DATA_CHUNK_BYTES, base_frame, data_b64

LINK_ID = "0123456789abcdef0123456789abcdef"
TITELLA = "lab"


class _ScriptedLink:
    """SerialLink stand-in: scriptable requests, dispatchable frames."""

    def __init__(self) -> None:
        self.link_id = LINK_ID
        self.connected = True
        self.state = "connected"
        self.port = "fake"
        self.handlers: list[tuple[str, Any]] = []
        self.state_handlers: list[Any] = []
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.sent: list[tuple[str, str, int, bytes]] = []
        self.streamed: list[tuple[str, str, list[bytes]]] = []
        self.request_handler: Any = None
        self.send_failure: Any = None

    def start(self) -> None:
        return None

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return True

    def subscribe(self, channel: str, handler: Any, session_id: str | None = None):
        entry = (channel, handler)
        self.handlers.append(entry)

        def unsubscribe() -> None:
            try:
                self.handlers.remove(entry)
            except ValueError:
                pass

        return unsubscribe

    def subscribe_state(self, handler: Any):
        self.state_handlers.append(handler)
        handler(self.state)

        def unsubscribe() -> None:
            try:
                self.state_handlers.remove(handler)
            except ValueError:
                pass

        return unsubscribe

    def emit_state(self, state: str) -> None:
        self.state = state
        for handler in list(self.state_handlers):
            handler(state)

    def emit(self, frame: dict[str, Any]) -> list[Any]:
        """Delivers one wire frame to the subscribers of its channel."""
        results = []
        for channel, handler in list(self.handlers):
            if frame.get("channel") == channel:
                results.append(handler(frame))
        return results

    def request(self, channel: str, op: str, body: dict[str, Any] | None = None, timeout: float = 6.0) -> dict[str, Any]:
        self.requests.append((channel, op, dict(body or {})))
        if self.request_handler is not None:
            return self.request_handler(channel, op, dict(body or {}))
        return {"body": {"ok": True}}

    def send_data(self, channel: str, session_id: str, seq: int, data: bytes) -> None:
        if self.send_failure is not None and self.send_failure(seq):
            raise TransportError(
                "ack_timeout", f"no ACK for {session_id}:{seq}", True, session_id=session_id,
            )
        self.sent.append((channel, session_id, seq, bytes(data)))

    def stream_data(self, channel: str, session_id: str, read_chunk: Any, window: int = 8,
                    on_ack: Any = None, stop_check: Any = None) -> int:
        blocks: list[bytes] = []
        while True:
            if stop_check is not None and stop_check():
                raise TransportError(
                    "cancelled", "transfer cancelled", False, session_id=session_id,
                )
            block = read_chunk()
            if not block:
                break
            blocks.append(block)
            if on_ack is not None:
                on_ack(sum(len(b) for b in blocks))
        self.streamed.append((channel, session_id, blocks))
        return sum(len(b) for b in blocks)

    def close(self, clean: bool = True) -> None:
        self.connected = False


class _ExplodingSink:
    """Audit sink whose every call raises: hooks must isolate it."""

    def __getattr__(self, name: str) -> Any:
        def explode(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"audit sink exploded in {name}")
        return explode


def _shell_data(session_id: str, seq: int, payload: bytes, stream: str = "stdout") -> dict[str, Any]:
    return base_frame(
        LINK_ID, "shell", "data", session_id=session_id, seq=seq,
        stream=stream, data_b64=data_b64(payload),
    )


def _shell_event(session_id: str, op: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return base_frame(
        LINK_ID, "shell", "event", session_id=session_id, op=op,
        body=dict(body or {}),
    )


def _transfer_data(transfer_id: str, seq: int, payload: bytes) -> dict[str, Any]:
    return base_frame(
        LINK_ID, "transfer", "data", session_id=transfer_id, seq=seq,
        data_b64=data_b64(payload),
    )


def _transfer_event(transfer_id: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
    return base_frame(
        LINK_ID, "transfer", "event", session_id=transfer_id, op=op, body=body,
    )


class AuditStreamsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audit_root = self.root / "audits"
        self.manager = AuditManager(self.audit_root)
        self.addCleanup(self.manager.close)
        self.manager.start()
        self.activated = self.manager.activate("streams", TITELLA)
        self.link = _ScriptedLink()
        self.gate = AgentGate()
        self.gate.set_ready(True)
        self.shells = ShellClient(self.link, self.gate)  # type: ignore[arg-type]
        self.files = FilesClient(self.link, self.gate)  # type: ignore[arg-type]
        self.addCleanup(self.files.close)
        self.addCleanup(self.shells.close)
        self.shells.audit_manager = self.manager
        self.shells.audit_titella_id = TITELLA
        self.files.audit_manager = self.manager
        self.files.audit_titella_id = TITELLA
        # Production wiring: the manager subscribes the shell channel and the
        # link-state transitions of this instance.
        self.link.subscribe(
            "shell", lambda frame: self.manager.record_shell_frame(TITELLA, frame)
        )
        self.link.subscribe_state(
            lambda state: self.manager.note_link_state(TITELLA, state)
        )
        self.call = AuditCall(
            action_id="a" * 32,
            audit_id="streams",
            run_id=self.activated["run_id"],
            titella_id=TITELLA,
            tool="remote_shell",
            started_monotonic=0.0,
            started_utc="2026-01-01T00:00:00Z",
        )
        self.call_token = set_current_audit_call(self.call)
        self.addCleanup(lambda: reset_current_audit_call(self.call_token))

    # ── evidence helpers ────────────────────────────────────────────────
    def _events(self) -> list[dict[str, Any]]:
        self.manager.flush_barrier()
        path = self.audit_root / "streams" / "events.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def _with_role(self, kind: str, role: str) -> list[dict[str, Any]]:
        return [
            event for event in self._events()
            if event.get("kind") == kind
            and (event.get("data") or {}).get("role") == role
        ]

    def _artifact_bytes(self, evidence_id: str) -> bytes:
        return (self.audit_root / "streams" / "artifacts" / f"{evidence_id}.bin").read_bytes()

    def _stream_for(self, role: str, operation_id: str | None = None) -> dict[str, Any]:
        matches = [
            event for event in self._with_role("evidence.created", role)
            if operation_id is None
            or (event.get("data") or {}).get("operation_id") == operation_id
        ]
        self.assertTrue(matches, f"no {role} stream for {operation_id!r}")
        return matches[-1]

    def _progress_events(self) -> list[dict[str, Any]]:
        return [
            event for event in self._events()
            if event.get("kind") == "operation.bound"
            and (event.get("data") or {}).get("observation") == "progress"
        ]

    def _stream_payload(self, session_id: str, payload: bytes, start_seq: int = 0,
                        stream: str = "stdout") -> int:
        seq = start_seq
        for offset in range(0, len(payload), DATA_CHUNK_BYTES):
            self.link.emit(
                _shell_data(session_id, seq, payload[offset:offset + DATA_CHUNK_BYTES], stream)
            )
            seq += 1
        return seq


class ShellJobAuditTests(AuditStreamsTestCase):
    def test_sync_job_keeps_full_streams_and_finishes_once(self) -> None:
        # > 1 MiB stdout whose frame boundary splits a two-byte UTF-8
        # character: \xc3 ends chunk 2047 and \xb1 opens chunk 2048. stderr
        # is also emitted; exit code 7 must survive.
        stdout = b"A" * (1048576 - 1) + b"\xc3" + b"\xb1" + b"B" * 100
        stderr = b"E" * 4099
        self.assertEqual(len(stdout), 1048677)

        def handler(channel: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
            if (channel, op) != ("shell", "job.start"):
                return {"body": {"ok": True}}
            job_id = body["job_id"]
            seq = self._stream_payload(job_id, stdout, 0)
            seq = self._stream_payload(job_id, stderr, seq, stream="stderr")
            self.link.emit(
                _shell_event(job_id, "job.finished",
                             {"status": "finished", "exit_code": 7})
            )
            return {"body": {"status": "started"}}

        self.link.request_handler = handler
        result = self.shells.remote_shell("probe", sync_timeout=5)

        self.assertEqual(result["status"], "finished")
        self.assertIsNone(result["job_id"])  # inline results hide the id
        self.assertEqual(result["exit_code"], 7)
        self.assertTrue(result["stdout_truncated"])

        events = self._events()
        started = [e for e in events if e["kind"] == "operation.started"]
        self.assertEqual(len(started), 1)
        job_id = started[0]["data"]["operation_id"]
        finished = [
            e for e in events if e["kind"] == "operation.finished"
            and e["data"].get("operation_id") == job_id
        ]
        self.assertEqual(len(finished), 1, "synchronous job finished twice")
        self.assertEqual(finished[0]["data"]["exit_code"], 7)

        out_stream = self._stream_for("stdout", job_id)
        self.assertEqual(
            self._artifact_bytes(out_stream["data"]["evidence_id"]), stdout
        )
        err_stream = self._stream_for("stderr", job_id)
        self.assertEqual(
            self._artifact_bytes(err_stream["data"]["evidence_id"]), stderr
        )
        closed = [
            e for e in events
            if e["kind"] == "stream.closed"
            and e["data"].get("evidence_id") == out_stream["data"]["evidence_id"]
        ]
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0]["data"]["complete"])

    def test_async_job_returns_without_polling_and_records_later(self) -> None:
        self.link.request_handler = lambda channel, op, body: {"body": {"status": "started"}}
        result = self.shells.remote_shell("slow", sync_timeout=0)

        # No wait, no poll: the call returns while the fake never finished.
        self.assertEqual(result["status"], "running")
        job_id = result["job_id"]
        self.assertTrue(isinstance(job_id, str))

        payload = b"x" * 2048
        self._stream_payload(job_id, payload, 0)
        # Identical duplicate adds no bytes; reused seq with other content is a gap.
        self.link.emit(_shell_data(job_id, 0, payload[:DATA_CHUNK_BYTES]))
        self.link.emit(_shell_data(job_id, 0, b"Z" * DATA_CHUNK_BYTES))
        self.link.emit(_shell_event(job_id, "job.finished", {"status": "finished", "exit_code": 0}))

        status = self.shells.shell_result(job_id)
        self.assertEqual(status["status"], "finished")

        events = self._events()
        stream = self._stream_for("stdout", job_id)
        self.assertEqual(self._artifact_bytes(stream["data"]["evidence_id"]), payload)
        finished = [
            e for e in events if e["kind"] == "operation.finished"
            and e["data"].get("operation_id") == job_id
        ]
        self.assertEqual(len(finished), 1)
        self.assertTrue([e for e in events if e["kind"] == "recording.gap"])
        self.assertEqual(self.manager.status(TITELLA)["pending"], [])

    def test_job_start_error_body_closes_the_operation(self) -> None:
        self.link.request_handler = lambda channel, op, body: {
            "body": {"status": "error", "code": "spawn_failed", "message": "no"}
        }
        result = self.shells.remote_shell("bad")
        self.assertEqual(result["status"], "error")
        events = self._events()
        finished = [e for e in events if e["kind"] == "operation.finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["data"]["status"], "error")
        self.assertEqual(self.manager.status(TITELLA)["pending"], [])
        self.assertEqual(self.shells.jobs, {})

    def test_job_start_delivery_failure_closes_the_operation(self) -> None:
        def handler(channel: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
            raise TransportError("request_timeout", "no answer", True)

        self.link.request_handler = handler
        with self.assertRaises(TransportError):
            self.shells.remote_shell("bad")
        events = self._events()
        finished = [e for e in events if e["kind"] == "operation.finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["data"]["status"], "failed")
        self.assertEqual(self.manager.status(TITELLA)["pending"], [])
        self.assertEqual(self.shells.jobs, {})


class TerminalAuditTests(AuditStreamsTestCase):
    def test_early_frames_bind_after_response_and_survive_the_ring(self) -> None:
        early = b"early-output-"
        stray = b"stray-session"

        def handler(channel: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
            if (channel, op) != ("shell", "terminal.open"):
                return {"body": {"ok": True}}
            # The slave streams before answering; another session is mixed in.
            self.link.emit(_shell_data("sess-early", 0, early))
            self.link.emit(_shell_data("sess-other", 0, stray))
            return {"body": {"session_id": "sess-early", "status": "opened"}}

        self.link.request_handler = handler
        body = self.shells.terminal_open(None, None, 24, 80, 60)
        self.assertEqual(body["session_id"], "sess-early")

        late = b"T" * (1024 * 1024 + 1234)
        self._stream_payload("sess-early", late, 1)
        self.link.emit(_shell_event("sess-early", "terminal.closed", {"status": "closed"}))

        events = self._events()
        bound = [e for e in events if e["kind"] == "operation.bound"
                 and e["data"].get("observation") != "progress"]
        self.assertEqual([e["data"]["session_id"] for e in bound], ["sess-early"])
        self.assertEqual(bound[0]["data"]["action_id"], self.call.action_id)
        started = [e for e in events if e["kind"] == "operation.started"]
        self.assertEqual([e["data"]["operation_id"] for e in started], ["sess-early"])
        self.assertEqual(started[0]["data"]["operation_kind"], "terminal")

        early_stream = self._stream_for("terminal_output", "sess-early")
        self.assertEqual(
            self._artifact_bytes(early_stream["data"]["evidence_id"]), early + late
        )
        stray_stream = self._stream_for("terminal_output", "sess-other")
        self.assertEqual(
            self._artifact_bytes(stray_stream["data"]["evidence_id"]), stray
        )
        # The client ring dropped the head; the evidence kept every byte.
        read = self.shells.terminal_read("sess-early", 0, 0, 65536, "utf-8")
        self.assertGreater(read["dropped_before"], 0)

    def test_failed_open_binds_nothing_and_keeps_early_frames_unresolved(self) -> None:
        def handler(channel: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
            if (channel, op) != ("shell", "terminal.open"):
                return {"body": {"ok": True}}
            self.link.emit(_shell_data("ghost", 0, b"orphan-bytes"))
            return {"body": {"status": "error", "code": "open_failed"}}

        self.link.request_handler = handler
        body = self.shells.terminal_open(None, None, 24, 80, 60)
        self.assertEqual(body["status"], "error")

        events = self._events()
        self.assertEqual([e for e in events if e["kind"] == "operation.bound"], [])
        self.assertEqual([e for e in events if e["kind"] == "operation.started"], [])
        self.assertEqual(self.shells.terminals, {})
        orphan = self._stream_for("terminal_output", "ghost")
        self.assertEqual(
            self._artifact_bytes(orphan["data"]["evidence_id"]), b"orphan-bytes"
        )


class TerminalWriteAuditTests(AuditStreamsTestCase):
    def _open(self) -> str:
        self.link.request_handler = lambda channel, op, body: (
            {"body": {"session_id": "w", "status": "opened"}}
            if (channel, op) == ("shell", "terminal.open")
            else {"body": {"ok": True}}
        )
        return str(self.shells.terminal_open(None, None, 24, 80, 60)["session_id"])

    def test_base64_input_is_archived_exactly_before_sends(self) -> None:
        session = self._open()
        payload = bytes((index * 7 + 3) % 256 for index in range(1300))
        encoded = base64.b64encode(payload).decode("ascii")

        result = self.shells.terminal_write(session, encoded, "base64")

        self.assertEqual(result["bytes"], len(payload))
        artifact = self._stream_for("terminal_input")["data"]
        self.assertTrue(artifact["sealed"])
        self.assertEqual(self._artifact_bytes(artifact["evidence_id"]), payload)
        sent = b"".join(chunk for _, _, _, chunk in self.link.sent)
        self.assertEqual(sent, payload)

        progress = self._progress_events()
        self.assertEqual([event["data"]["sent_bytes"] for event in progress],
                         [512, 1024, 1300])
        self.assertTrue(progress[-1]["data"]["complete"])

    def test_partial_send_keeps_requested_input_and_partial_progress(self) -> None:
        session = self._open()
        payload = bytes((index * 11 + 5) % 256 for index in range(1300))
        encoded = base64.b64encode(payload).decode("ascii")
        self.link.send_failure = lambda seq: seq == 1

        with self.assertRaises(TransportError):
            self.shells.terminal_write(session, encoded, "base64")

        artifact = self._stream_for("terminal_input")["data"]
        self.assertEqual(self._artifact_bytes(artifact["evidence_id"]), payload)
        progress = self._progress_events()
        self.assertTrue(progress)
        last = progress[-1]["data"]
        self.assertEqual(last["sent_bytes"], 512)
        self.assertFalse(last["complete"])
        self.assertTrue(last.get("partial"))
        self.assertEqual(b"".join(chunk for _, _, _, chunk in self.link.sent),
                         payload[:DATA_CHUNK_BYTES])

    def test_exploding_sink_blocks_the_write_before_any_byte_is_sent(self) -> None:
        session = self._open()
        self.shells.audit_manager = _ExplodingSink()
        payload = b"z" * 1200

        # The requested input is recorded before the sends: a store that
        # cannot record it fails the audited call closed, with zero bytes
        # reaching the terminal.
        with self.assertRaises(RuntimeError):
            self.shells.terminal_write(
                session, base64.b64encode(payload).decode("ascii"), "base64"
            )
        self.assertEqual(self.link.sent, [])


class TransferAuditTests(AuditStreamsTestCase):
    def test_upload_records_streamed_bytes_despite_source_mutation(self) -> None:
        payload = bytes((index * 31 + 7) % 256 for index in range(65553))
        source = self.root / "upload.bin"
        source.write_bytes(payload)
        self.link.request_handler = lambda channel, op, body: {
            "body": {"ok": True, "status": "finished"}
        }

        result = self.files.upload(str(source), "remote/upload.bin", sync_timeout=10)
        transfer_id = result["transfer_id"]
        self.assertEqual(result["status"], "finished")

        # The source is mutated after the send: archived bytes must not move.
        source.write_bytes(b"tampered" * 100)

        events = self._events()
        stream = self._stream_for("upload", transfer_id)
        archived = self._artifact_bytes(stream["data"]["evidence_id"])
        self.assertEqual(archived, payload)
        self.assertEqual(hashlib.sha256(archived).hexdigest(),
                         hashlib.sha256(payload).hexdigest())
        blocks = self.link.streamed[-1][2]
        self.assertEqual(b"".join(blocks), payload)
        closed = [e for e in events if e["kind"] == "stream.closed"
                  and e["data"].get("evidence_id") == stream["data"]["evidence_id"]]
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0]["data"]["complete"])
        self.assertEqual(closed[0]["data"]["status"]["bytes_transferred"], len(payload))

    def _pump_download(self, transfer_id: str, payload: bytes) -> None:
        seq = 0
        for offset in range(0, len(payload), DATA_CHUNK_BYTES):
            self.link.emit(
                _transfer_data(transfer_id, seq, payload[offset:offset + DATA_CHUNK_BYTES])
            )
            seq += 1
        self.link.emit(
            _transfer_event(transfer_id, "transfer.finished",
                            {"status": "finished", "bytes": len(payload)})
        )

    def test_download_round_trip_records_transmitted_bytes(self) -> None:
        payload = bytes((index * 17 + 3) % 256 for index in range(65553))
        begun = threading.Event()

        def handler(channel: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
            if (channel, op) == ("transfer", "download.begin"):
                # The worker created its temp stream before this request, so
                # once it is observed the receiver can accept frames.
                begun.set()
                return {"body": {"status": "started", "bytes_total": len(payload),
                                 "sha256": hashlib.sha256(payload).hexdigest()}}
            return {"body": {"ok": True}}

        self.link.request_handler = handler
        local = self.root / "download.bin"
        result = self.files.download("remote/download.bin", str(local), sync_timeout=0)
        transfer_id = result["transfer_id"]
        transfer = self.files._transfers[transfer_id]
        self.assertTrue(begun.wait(10))

        self._pump_download(transfer_id, payload)
        self.assertTrue(transfer.done.wait(10))
        status = self.files.transfer_status(transfer_id)
        self.assertEqual(status["status"], "finished")
        self.assertEqual(local.read_bytes(), payload)
        stream = self._stream_for("download", transfer_id)
        self.assertEqual(self._artifact_bytes(stream["data"]["evidence_id"]), payload)

    def test_checksum_failure_keeps_partial_evidence_and_verified_status(self) -> None:
        payload = bytes((index * 13 + 9) % 256 for index in range(65553))
        wrong = hashlib.sha256(b"other").hexdigest()
        begun = threading.Event()

        def handler(channel: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
            if (channel, op) == ("transfer", "download.begin"):
                begun.set()
                return {"body": {"status": "started", "bytes_total": len(payload),
                                 "sha256": wrong}}
            return {"body": {"ok": True}}

        self.link.request_handler = handler
        local = self.root / "checksum.bin"
        result = self.files.download("remote/checksum.bin", str(local), sync_timeout=0)
        transfer_id = result["transfer_id"]
        transfer = self.files._transfers[transfer_id]
        self.assertTrue(begun.wait(10))

        self._pump_download(transfer_id, payload)
        self.assertTrue(transfer.done.wait(10))

        # The wire body said "finished"; local verification says otherwise.
        self.assertEqual(transfer.result.get("status"), "finished")
        status = self.files.transfer_status(transfer_id)
        self.assertEqual(status["status"], "error")
        self.assertEqual(status["error"]["code"], "checksum_mismatch")
        self.assertFalse(local.exists())  # partial deleted locally

        events = self._events()
        stream = self._stream_for("download", transfer_id)
        self.assertEqual(self._artifact_bytes(stream["data"]["evidence_id"]), payload)
        closed = [e for e in events if e["kind"] == "stream.closed"
                  and e["data"].get("evidence_id") == stream["data"]["evidence_id"]]
        self.assertEqual(len(closed), 1)
        self.assertFalse(closed[0]["data"]["complete"])
        self.assertEqual(closed[0]["data"]["status"]["error"]["code"], "checksum_mismatch")

    def test_sink_failure_never_changes_the_download_ack(self) -> None:
        payload = b"k" * 1024
        begun = threading.Event()

        def handler(channel: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
            if (channel, op) == ("transfer", "download.begin"):
                begun.set()
                return {"body": {"status": "started", "bytes_total": len(payload),
                                 "sha256": hashlib.sha256(payload).hexdigest()}}
            return {"body": {"ok": True}}

        self.link.request_handler = handler
        local = self.root / "sink.bin"
        result = self.files.download("remote/sink.bin", str(local), sync_timeout=0)
        transfer_id = result["transfer_id"]
        transfer = self.files._transfers[transfer_id]
        self.assertTrue(begun.wait(10))
        self.files.audit_manager = _ExplodingSink()

        for seq, offset in enumerate(range(0, len(payload), 512)):
            results = self.link.emit(_transfer_data(transfer_id, seq, payload[offset:offset + 512]))
            self.assertEqual(results, [True])  # Every wire ACK survives the sink failure.
        self.link.emit(_transfer_event(transfer_id, "transfer.finished",
                                       {"status": "finished", "bytes": len(payload)}))
        self.assertTrue(transfer.done.wait(10))
        self.assertEqual(self.files.transfer_status(transfer_id)["status"], "finished")
        self.assertEqual(local.read_bytes(), payload)


class FaultAndDisconnectTests(AuditStreamsTestCase):
    def test_faulted_store_keeps_shell_callbacks_true(self) -> None:
        self.link.request_handler = lambda channel, op, body: {"body": {"status": "started"}}
        result = self.shells.remote_shell("slow", sync_timeout=0)
        job_id = result["job_id"]

        self.manager.close()  # writer shut down: every enqueue now fails
        returns = self.link.emit(_shell_data(job_id, 0, b"after-fault"))
        self.assertTrue(all(value is True for value in returns))
        self.link.emit(_shell_event(job_id, "job.finished", {"status": "finished", "exit_code": 0}))
        self.assertEqual(self.shells.shell_result(job_id)["status"], "finished")

    def test_disconnect_without_finish_records_unknown(self) -> None:
        self.link.request_handler = lambda channel, op, body: {"body": {"status": "started"}}
        result = self.shells.remote_shell("slow", sync_timeout=0)
        job_id = result["job_id"]
        self.link.emit(_shell_data(job_id, 0, b"partial"))

        self.link.emit_state("disconnected")

        self.assertEqual(self.shells.shell_result(job_id)["status"], "disconnected")
        events = self._events()
        self.assertEqual(
            [e for e in events if e["kind"] == "operation.finished"
             and e["data"].get("operation_id") == job_id],
            [],
            "a disconnected job must never be recorded as finished",
        )
        interrupted = [
            e for e in events if e["kind"] == "operation.interrupted"
            and e["data"].get("operation_id") == job_id
        ]
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0]["data"]["status"], "unknown")
        stream = self._stream_for("stdout", job_id)
        closed = [e for e in events if e["kind"] == "stream.closed"
                  and e["data"].get("evidence_id") == stream["data"]["evidence_id"]]
        self.assertEqual(len(closed), 1)
        self.assertFalse(closed[0]["data"]["complete"])

    def test_no_evidence_is_written_while_the_audit_is_inactive(self) -> None:
        # Deactivate first: no binding exists, so neither the clients (no call
        # identity) nor the manager's shell subscriber may record anything.
        self.manager.deactivate(TITELLA)
        quiet_token = set_current_audit_call(None)
        self.addCleanup(lambda: reset_current_audit_call(quiet_token))
        before = len(self._events())

        self.link.request_handler = lambda channel, op, body: (
            {"body": {"session_id": "quiet", "status": "opened"}}
            if (channel, op) == ("shell", "terminal.open")
            else {"body": {"status": "started"}}
        )
        session = self.shells.terminal_open(None, None, 24, 80, 60)["session_id"]
        self.shells.terminal_write(session, base64.b64encode(b"silence").decode(), "base64")
        self.link.emit(_shell_data(session, 0, b"unrecorded"))
        self.shells.remote_shell("unrecorded", sync_timeout=0)

        after = self._events()
        self.assertEqual(len(after), before)
        self.assertEqual([e for e in after if e["kind"] in
                          {"evidence.created", "operation.started", "operation.finished"}], [])


if __name__ == "__main__":
    unittest.main()
