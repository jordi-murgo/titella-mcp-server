"""Regression tests for the master side of the file-transfer window.

Covers the bounded sender window with delayed and out-of-order ACKs, the
resend on a lost ACK with no duplicate blocks, the rejection of gaps
without a false ACK, the empty file and the file with a partial final
block, cancellation and the cleanup of the ACK registry, plus the
FilesClient download receiver on a deterministic fake link.
"""
from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace

from titella.agent_state import AgentGate
from titella.config import load_config
from titella.files_client import FilesClient, TransferState
from titella.serial_link import SerialLink, TransportError
from titella_slave_assets.wire import (
    Deduplicator,
    base_frame,
    data_b64,
    decode_data,
)


LINK = "0123456789abcdef0123456789abcdef"


class QueueOnlyLink(SerialLink):
    """SerialLink with no real threads: only frame recording and future ACKs."""

    def start(self) -> None:
        self._started.set()


class RecordingLink(QueueOnlyLink):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.frames: list[dict] = []
        self.condition = threading.Condition()

    def send_frame(self, frame) -> None:
        with self.condition:
            self.frames.append(dict(frame))
            self.condition.notify_all()

    def data_frames(self, channel: str = "transfer") -> list[dict]:
        with self.condition:
            return [f for f in self.frames if f["type"] == "data" and f["channel"] == channel]

    def ack_frames(self, session_id: str) -> list[dict]:
        with self.condition:
            return [
                f for f in self.frames
                if f["type"] == "ack" and f["session_id"] == session_id
            ]

    def wait_frames(self, count: int, channel: str = "transfer", timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while len(self.data_frames(channel)) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"expected {count} data frames, got {len(self.data_frames(channel))}"
                    )
                self.condition.wait(remaining)

    def deliver_ack(self, session_id: str, seq: int, channel: str = "transfer") -> None:
        """Injects an ACK through the link's real dispatch path."""
        self._dispatch(base_frame(
            self.link_id, channel, "ack", session_id=session_id, ack_seq=seq,
        ))


class WindowSenderTests(unittest.TestCase):
    def config(self, temp: str, *, timeout: float = 1.0, retries: int = 3):
        return replace(
            load_config(
                os.path.join(temp, "absent.toml"),
                {"TITELLA_STATE_DIR": temp, "TITELLA_HEARTBEAT_S": "5", "TITELLA_DISCONNECT_GRACE_S": "30"},
            ),
            ack_timeout_s=timeout,
            ack_retries=retries,
        )

    def link(self, temp: str, **kwargs) -> RecordingLink:
        link = RecordingLink(self.config(temp, **kwargs))
        self.addCleanup(link.close)
        return link

    @staticmethod
    def chunks(count: int, size: int = 512) -> list[bytes]:
        return [bytes([index % 251]) * size for index in range(count)]

    @staticmethod
    def start_stream(link, session_id, source, **kwargs) -> tuple[threading.Thread, dict]:
        iterator = iter(source)
        result: dict = {}

        def run() -> None:
            try:
                result["acked"] = link.stream_data(
                    "transfer", session_id, lambda: next(iterator, b""), **kwargs
                )
            except BaseException as exc:  # the test checks the concrete type
                result["error"] = exc

        worker = threading.Thread(target=run)
        worker.start()
        return worker, result

    def drain(self, link: RecordingLink, worker: threading.Thread, session_id: str) -> None:
        """Acknowledges each frame as soon as it appears, honouring arrival order."""
        acked: set[int] = set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for frame in link.data_frames():
                if frame["seq"] not in acked:
                    acked.add(frame["seq"])
                    link.deliver_ack(session_id, frame["seq"])
            if not worker.is_alive():
                return
            time.sleep(0.002)
        self.fail("the sender did not finish within the deadline")

    def test_window_bounds_inflight_and_releases_slots_progressively(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = self.link(temp, timeout=5.0)
            source = self.chunks(20)
            worker, result = self.start_stream(link, "stream", source, window=8)
            try:
                # With no ACK at all the window fills to 8 and does not go past that.
                link.wait_frames(8)
                self.assertEqual(len(link.data_frames()), 8)
                # An out-of-order ACK releases exactly one slot.
                link.deliver_ack("stream", 5)
                link.wait_frames(9)
                self.assertEqual(link.data_frames()[-1]["seq"], 8)
                link.deliver_ack("stream", 0)
                link.wait_frames(10)
                self.assertEqual(link.data_frames()[-1]["seq"], 9)
                # The rest is acknowledged progressively and without duplicates.
                self.drain(link, worker, session_id="stream")
            finally:
                worker.join(5)
            self.assertFalse(
                worker.is_alive(), "the sender must finish once everything is acknowledged",
            )
            self.assertNotIn("error", result)
            self.assertEqual(result["acked"], 20 * 512)
            self.assertEqual([f["seq"] for f in link.data_frames()], list(range(20)))
            self.assertEqual(link._acks, {})

    def test_window_one_behaves_as_stop_and_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = self.link(temp, timeout=5.0)
            worker, result = self.start_stream(link, "one", self.chunks(3), window=1)
            try:
                link.wait_frames(1)
                time.sleep(0.15)
                self.assertEqual(
                    len(link.data_frames()), 1, "window=1 must not run ahead of blocks",
                )
                for seq in range(3):
                    if seq:
                        link.wait_frames(seq + 1)
                    link.deliver_ack("one", seq)
            finally:
                worker.join(5)
            self.assertNotIn("error", result)
            self.assertEqual([f["seq"] for f in link.data_frames()], [0, 1, 2])
            self.assertEqual(result["acked"], 3 * 512)

    def test_lost_ack_retransmits_same_block_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = self.link(temp, timeout=0.3, retries=3)
            worker, result = self.start_stream(link, "retry", self.chunks(3), window=1)
            try:
                link.wait_frames(1)  # seq 0: first send, ACK lost
                link.wait_frames(2)  # resend of the same seq 0
                self.assertEqual([f["seq"] for f in link.data_frames()], [0, 0])
                link.deliver_ack("retry", 0)
                link.wait_frames(3)
                self.assertEqual(link.data_frames()[-1]["seq"], 1)
                link.deliver_ack("retry", 1)
                link.wait_frames(4)
                link.deliver_ack("retry", 2)
            finally:
                worker.join(5)
            self.assertNotIn("error", result)
            self.assertEqual([f["seq"] for f in link.data_frames()], [0, 0, 1, 2])
            self.assertEqual(result["acked"], 3 * 512)
            self.assertEqual(link._acks, {})

    def test_exhausted_retries_raise_ack_timeout_and_clear_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = self.link(temp, timeout=0.01, retries=2)
            worker, result = self.start_stream(link, "dead", self.chunks(2), window=2)
            worker.join(5)
            self.assertFalse(worker.is_alive())
            error = result.get("error")
            self.assertIsInstance(error, TransportError)
            self.assertEqual(error.code, "ack_timeout")
            # Initial send plus two resends of each block in the window.
            per_seq = [f["seq"] for f in link.data_frames()]
            self.assertEqual(per_seq.count(0), 3)
            self.assertEqual(per_seq.count(1), 3)
            self.assertEqual(link._acks, {}, "the ACK registry must be left clean")

    def test_empty_stream_and_partial_final_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = self.link(temp)
            self.assertEqual(link.stream_data("transfer", "empty", lambda: b""), 0)
            self.assertEqual(link.data_frames(), [])

            source = [b"x" * 512, b"y" * 8]
            worker, result = self.start_stream(link, "partial", source, window=8)
            try:
                link.wait_frames(2)
                link.deliver_ack("partial", 0)
                link.deliver_ack("partial", 1)
            finally:
                worker.join(5)
            self.assertNotIn("error", result)
            self.assertEqual([len(decode_data(f)) for f in link.data_frames()], [512, 8])
            self.assertEqual(result["acked"], 520)
            self.assertEqual(link._acks, {})

    def test_stop_check_aborts_and_cleans_pending_acks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = self.link(temp, timeout=0.1, retries=3)
            cancelled = threading.Event()
            worker, result = self.start_stream(
                link, "cancel", self.chunks(20), window=8, stop_check=cancelled.is_set,
            )
            link.wait_frames(8)
            cancelled.set()
            worker.join(3)
            self.assertFalse(
                worker.is_alive(), "cancellation must not leave the sender waiting for an ACK",
            )
            error = result.get("error")
            self.assertIsInstance(error, TransportError)
            self.assertEqual(error.code, "cancelled")
            self.assertEqual(link._acks, {}, "no ACK must remain registered")
            self.assertLessEqual(
                len(link.data_frames()), 16, "at most one extra round of resends",
            )

    def test_close_terminates_inflight_waiters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = self.link(temp, timeout=5.0, retries=3)
            worker, result = self.start_stream(link, "closing", self.chunks(20), window=4)
            link.wait_frames(4)
            link.close()
            worker.join(3)
            self.assertFalse(worker.is_alive(), "close must wake the senders waiting for an ACK")
            error = result.get("error")
            self.assertIsInstance(error, TransportError)
            self.assertEqual(error.code, "shutdown")
            self.assertEqual(link._acks, {})

    def test_stream_does_not_hold_data_lock_for_interactive_sends(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = self.link(temp, timeout=3.0, retries=3)
            worker, _result = self.start_stream(link, "big", self.chunks(40), window=8)
            link.wait_frames(8)
            sent: dict = {"done": False, "error": None}

            def interactive() -> None:
                try:
                    link.send_data("shell", "pty", 9, b"input")
                    sent["done"] = True
                except BaseException as exc:
                    sent["error"] = exc

            talker = threading.Thread(target=interactive)
            talker.start()
            deadline = time.monotonic() + 3
            while not any(f["seq"] == 9 for f in link.data_frames("shell")):
                self.assertLess(time.monotonic(), deadline, "the interactive send did not arrive")
                time.sleep(0.005)
            link.deliver_ack("pty", 9, channel="shell")
            talker.join(3)
            self.assertFalse(talker.is_alive(), "the terminal must not be blocked by the file")
            self.assertIsNone(sent["error"])
            self.assertTrue(sent["done"])
            self.assertTrue(worker.is_alive(), "the file stream is still waiting for its ACK")
            link.close()
            worker.join(3)
            self.assertFalse(worker.is_alive())


class ReceiverTests(unittest.TestCase):
    def config(self, temp: str):
        return replace(
            load_config(
                os.path.join(temp, "absent.toml"),
                {"TITELLA_STATE_DIR": temp, "TITELLA_HEARTBEAT_S": "5", "TITELLA_DISCONNECT_GRACE_S": "30"},
            ),
            ack_timeout_s=1.0,
            ack_retries=3,
        )

    def test_rejected_data_frame_is_not_deduped_and_retry_is_acked(self) -> None:
        """Registering the key before accepting would produce a false ACK."""
        with tempfile.TemporaryDirectory() as temp:
            link = RecordingLink(self.config(temp))
            self.addCleanup(link.close)
            accepted: list[int] = []
            accept_first = [False]

            def handler(frame: dict) -> bool:
                if frame["seq"] in accepted:
                    self.fail("the duplicate must not be reapplied")
                if not accept_first[0]:
                    accept_first[0] = True
                    return False
                accepted.append(frame["seq"])
                return True

            link.subscribe("transfer", handler, session_id="sess")
            frame = base_frame(
                link.link_id, "transfer", "data",
                session_id="sess", seq=0, data_b64=data_b64(b"x" * 512),
            )
            link._dispatch(frame)
            self.assertEqual(link.ack_frames("sess"), [], "a rejected frame is not acknowledged")
            # The resend of the same block must be acceptable: the key must
            # not have been registered when the first one was rejected.
            link._dispatch(frame)
            self.assertEqual(accepted, [0])
            self.assertEqual(len(link.ack_frames("sess")), 1)
            # A duplicate already applied is acknowledged again without being reapplied.
            link._dispatch(frame)
            self.assertEqual(accepted, [0])
            self.assertEqual(len(link.ack_frames("sess")), 2)

    def test_download_receiver_writes_only_the_expected_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = RecordingLink(self.config(temp))
            self.addCleanup(link.close)
            files = FilesClient(link, AgentGate())
            transfer = TransferState("down", "download")
            transfer.stream = io.BytesIO()
            files._transfers["down"] = transfer

            def frame(seq: int, payload: bytes) -> dict:
                return base_frame(
                    link.link_id, "transfer", "data",
                    session_id="down", seq=seq, data_b64=data_b64(payload),
                )

            self.assertTrue(files._on_transfer(frame(0, b"a" * 512)))
            self.assertFalse(files._on_transfer(frame(2, b"c" * 512)), "a gap is not written")
            self.assertEqual(transfer.stream.getvalue(), b"a" * 512)
            self.assertTrue(files._on_transfer(frame(1, b"b" * 512)))
            self.assertTrue(files._on_transfer(frame(2, b"c" * 512)))
            self.assertEqual(transfer.next_seq, 3)
            self.assertEqual(
                transfer.stream.getvalue(), b"a" * 512 + b"b" * 512 + b"c" * 512,
            )

    def test_late_duplicate_after_dedup_eviction_is_acked_without_rewrite(self) -> None:
        """A lost ACK with progressive ACKs can push the block out of the dedup set."""
        with tempfile.TemporaryDirectory() as temp:
            link = RecordingLink(self.config(temp))
            self.addCleanup(link.close)
            files = FilesClient(link, AgentGate())
            transfer = TransferState("down", "download")
            transfer.stream = io.BytesIO()
            files._transfers["down"] = transfer

            frame = base_frame(
                link.link_id, "transfer", "data",
                session_id="down", seq=0, data_b64=data_b64(b"a" * 512),
            )
            link._dispatch(frame)
            self.assertEqual(transfer.next_seq, 1)
            self.assertEqual(len(link.ack_frames("down")), 1)

            # The block leaves the global dedup window (as after >1024
            # sequences) and the late resend arrives: it must be acknowledged
            # without rewriting bytes.
            link._dedup = Deduplicator(limit=1)
            link._dedup.add((link.link_id, "other-session", 0))
            self.assertFalse(link._dedup.seen((link.link_id, "down", 0)))

            link._dispatch(frame)
            self.assertEqual(transfer.next_seq, 1, "an already applied block must not be rewritten")
            self.assertEqual(len(transfer.stream.getvalue()), 512)
            self.assertEqual(len(link.ack_frames("down")), 2, "it must acknowledge again")


if __name__ == "__main__":
    unittest.main()
