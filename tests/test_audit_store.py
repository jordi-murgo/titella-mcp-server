"""Behavioral tests of the audit store: activation, privacy, recovery, faults."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from titella.audit import (
    AuditCall,
    AuditError,
    AuditManager,
    validate_audit_id,
    utc_now_rfc3339,
)


def _call(manager: AuditManager, audit_id: str, run_id: str, titella: str, tool: str = "remote_shell") -> AuditCall:
    return AuditCall(
        action_id="a" * 32,
        audit_id=audit_id,
        run_id=run_id,
        titella_id=titella,
        tool=tool,
        started_monotonic=0.0,
        started_utc=utc_now_rfc3339(),
    )


class ActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "audits"
        self.manager = AuditManager(self.root)
        self.addCleanup(self.manager.close)
        self.manager.start()

    def test_failed_deactivation_retains_fail_closed_binding(self) -> None:
        self.manager.activate("case", "lab")
        persisted = (self.root / "active.json").read_bytes()
        with patch.object(self.manager, "_persist_bindings", side_effect=PermissionError("read-only root")):
            with self.assertRaises(AuditError):
                self.manager.deactivate("lab")
        self.assertEqual(self.manager.status("lab")["status"], "faulted")
        self.assertEqual((self.root / "active.json").read_bytes(), persisted)
        with self.assertRaises(AuditError) as caught:
            self.manager.begin_call("lab", "type", {"text": "must not execute"})
        self.assertEqual(caught.exception.code, "audit_recording_failed")

    def test_deactivating_interrupted_run_records_unknown_not_success(self) -> None:
        self.manager.activate("case", "lab")
        call = self.manager.begin_call("lab", "remote_shell", {"command": "unfinished"})
        self.manager.close()
        recovered = AuditManager(self.root)
        self.addCleanup(recovered.close)
        recovered.start()
        self.assertEqual(recovered.status("lab")["status"], "recovery_required")
        self.assertEqual(recovered.deactivate("lab")["status"], "inactive")
        events = [json.loads(line) for line in (self.root / "case" / "events.jsonl").read_text().splitlines()]
        interrupted = [event for event in events if event["kind"] == "operation.interrupted" and event["action_id"] == call.action_id]
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0]["data"]["status"], "unknown")

    def test_two_instances_two_audits_exact_separation(self) -> None:
        alpha = self.manager.activate("audit-a", "alpha")
        self.assertEqual(alpha["status"], "active")
        beta = self.manager.activate("audit-b", "beta")
        # beta events never land in audit-a
        beta_call = _call(self.manager, "audit-b", beta["run_id"], "beta")
        stream = self.manager.open_stream(
            beta_call, role="stdout", channel="shell",
            operation_id="job-beta", titella_id="beta",
        )
        self.assertIsNotNone(stream)
        self.manager.append_stream(stream, b"beta-only\n")
        self.manager.close_stream(stream, complete=True, reason="job.finished:ok")
        self.manager.flush_barrier()
        beta_bin = (self.root / "audit-b" / "artifacts" / f"{stream}.bin").read_bytes()
        self.assertEqual(beta_bin, b"beta-only\n")
        alpha_artifacts = list((self.root / "audit-a" / "artifacts").iterdir())
        for artifact in alpha_artifacts:
            if artifact.suffix == ".bin":
                self.assertNotIn(b"beta-only", artifact.read_bytes())

    def test_reactivation_same_id_same_run_no_duplicate_event(self) -> None:
        first = self.manager.activate("same", "alpha")
        before = (self.root / "same" / "events.jsonl").read_text()
        second = self.manager.activate("same", "alpha")
        self.assertEqual(first["run_id"], second["run_id"])
        after = (self.root / "same" / "events.jsonl").read_text()
        self.assertEqual(before, after)

    def test_different_id_is_already_active(self) -> None:
        self.manager.activate("one", "alpha")
        with self.assertRaises(AuditError) as ctx:
            self.manager.activate("two", "alpha")
        self.assertEqual(ctx.exception.code, "audit_already_active")

    def test_deactivate_with_pending_call_is_busy(self) -> None:
        activated = self.manager.activate("busy", "alpha")
        call = self.manager.begin_call("alpha", "remote_shell", {"cmd": "x", "titella": "alpha"})
        self.assertIsNotNone(call)
        with self.assertRaises(AuditError) as ctx:
            self.manager.deactivate("alpha")
        self.assertEqual(ctx.exception.code, "audit_busy")
        self.manager.finish_call(call, result={"ok": True})
        self.manager.flush_barrier()
        result = self.manager.deactivate("alpha")
        self.assertEqual(result["status"], "inactive")
        self.assertEqual(result["audit_id"], "busy")

    def test_deactivate_inactive_returns_null_audit(self) -> None:
        result = self.manager.deactivate("gamma")
        self.assertIsNone(result["audit_id"])

    def test_restart_marks_recovery_required_and_resume_opens_new_run(self) -> None:
        first = self.manager.activate("resume-me", "alpha")
        self.manager.close()
        second_manager = AuditManager(self.root)
        self.addCleanup(second_manager.close)
        second_manager.start()
        status = second_manager.status("alpha")
        self.assertEqual(status["status"], "recovery_required")
        resumed = second_manager.activate("resume-me", "alpha")
        self.assertNotEqual(resumed["run_id"], first["run_id"])
        events = (self.root / "resume-me" / "events.jsonl").read_text()
        self.assertIn("daemon.interrupted", events)

    def test_private_permissions_and_traversal_rejected(self) -> None:
        self.manager.activate("perms", "alpha")
        audit_dir = self.root / "perms"
        self.assertEqual(stat.S_IMODE(audit_dir.stat().st_mode), 0o700)
        events_mode = stat.S_IMODE((audit_dir / "events.jsonl").stat().st_mode)
        self.assertEqual(events_mode, 0o600)
        # a foreign directory is never adopted (another instance: alpha is
        # already bound, and audit_already_active has precedence there)
        foreign = self.root / "foreign"
        foreign.mkdir(mode=0o700)
        (foreign / "audit.json").write_text("not json")
        with self.assertRaises(AuditError) as ctx:
            self.manager.activate("foreign", "beta")
        self.assertEqual(ctx.exception.code, "audit_invalid_directory")
        # traversal in the audit name is rejected by the id grammar
        with self.assertRaises(AuditError):
            validate_audit_id("../escape")

    def test_symlink_inside_archive_rejected(self) -> None:
        self.manager.activate("links", "alpha")
        self.manager.flush_barrier()
        target = Path(self.temp.name) / "outside.txt"
        target.write_text("x")
        try:
            os.symlink(target, self.root / "links" / "artifacts" / "evil.bin")
        except OSError:
            self.skipTest("symlinks unavailable")
        # Release the writer lock so the second manager validates the archive.
        self.manager.close()
        manager2 = AuditManager(self.root)
        self.addCleanup(manager2.close)
        manager2.start()
        with self.assertRaises(AuditError) as ctx:
            manager2.activate("links", "alpha")
        self.assertIn(ctx.exception.code, {"audit_invalid_directory", "audit_root_busy"})
        if ctx.exception.code == "audit_root_busy":
            self.fail("expected archive validation before writer contention")

    def test_insecure_directory_permissions_rejected(self) -> None:
        loose = self.root / "loose"
        loose.mkdir(mode=0o755, parents=True)
        with self.assertRaises(AuditError) as ctx:
            self.manager.activate("loose", "alpha")
        self.assertEqual(ctx.exception.code, "audit_invalid_directory")


class WriterFaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "audits"

    def test_second_writer_process_is_root_busy(self) -> None:
        first = AuditManager(self.root)
        self.addCleanup(first.close)
        first.start()
        first.activate("dup", "alpha")
        second = AuditManager(self.root)
        self.addCleanup(second.close)
        second.start()
        with self.assertRaises(AuditError) as ctx:
            second.activate("other", "beta")
        self.assertEqual(ctx.exception.code, "audit_root_busy")

    def test_writer_failure_marks_faulted_and_blocks_new_calls(self) -> None:
        manager = AuditManager(self.root)
        self.addCleanup(manager.close)
        manager.start()
        activated = manager.activate("faulty", "alpha")
        # Poison the events file so appends fail: replace it with a directory.
        events = self.root / "faulty" / "events.jsonl"
        events.unlink()
        events.mkdir()
        with self.assertRaises(AuditError):
            manager.begin_call("alpha", "remote_shell", {"cmd": "x"})
        status = manager.status("alpha")
        self.assertEqual(status["status"], "faulted")
        with self.assertRaises(AuditError) as denied:
            manager.begin_call("alpha", "remote_shell", {"cmd": "y"})
        self.assertEqual(denied.exception.code, "audit_recording_failed")

    def test_shell_subscriber_never_raises_and_always_true(self) -> None:
        manager = AuditManager(self.root)
        self.addCleanup(manager.close)
        manager.start()
        manager.activate("sink", "alpha")
        # frames from an unbound session are ignored safely
        self.assertTrue(manager.record_shell_frame("alpha", {"type": "data", "session_id": "nope", "stream": "stdout", "data": ""}))
        self.assertTrue(manager.record_shell_frame("ghost", {"type": "event", "op": "job.finished", "session_id": "x"}))

    def test_full_queue_faults_synchronously_without_blocking_reader(self) -> None:
        from unittest.mock import patch
        manager = AuditManager(self.root)
        self.addCleanup(manager.close)
        manager.activate("queue", "alpha")
        with patch("titella.audit.MAX_QUEUE_BYTES", 0):
            self.assertTrue(manager.record_shell_frame("alpha", {"type": "event", "session_id": "job", "op": "job.finished", "body": {}}))
            self.assertEqual(manager.status("alpha")["status"], "faulted")
        manager.flush_barrier()
        with self.assertRaises(AuditError) as rejected:
            manager.begin_call("alpha", "remote_shell", {"cmd": "never sent"})
        self.assertEqual(rejected.exception.code, "audit_recording_failed")
        self.assertIn("recording.gap", (self.root / "queue" / "events.jsonl").read_text())

    def test_reader_does_not_wait_for_writer_barrier(self) -> None:
        manager = AuditManager(self.root)
        self.addCleanup(manager.close)
        manager.activate("reader", "alpha")
        entered, release, callback_done = threading.Event(), threading.Event(), threading.Event()
        def hold_writer() -> None:
            entered.set()
            release.wait(5)
        manager._submit(hold_writer)
        self.assertTrue(entered.wait(2))
        def callback() -> None:
            try:
                manager.record_shell_frame("alpha", {"type": "event", "session_id": "early", "op": "terminal.closed", "body": {}})
            finally:
                callback_done.set()
        worker = threading.Thread(target=callback)
        worker.start()
        try:
            self.assertTrue(callback_done.wait(1), "transport callback waited on the writer")
        finally:
            release.set()
            worker.join(2)
        manager.flush_barrier()

    def test_corrupt_binding_cannot_be_treated_as_inactive(self) -> None:
        self.root.mkdir(mode=0o700)
        path = self.root / "active.json"
        path.write_text('{"schema_version":1,"bindings":{"alpha":{"audit_id":"../bad","run_id":"bad"}}}')
        path.chmod(0o600)
        original = path.read_bytes()
        manager = AuditManager(self.root)
        self.addCleanup(manager.close)
        manager.start()
        with self.assertRaises(AuditError):
            manager.begin_call("alpha", "remote_shell", {"cmd": "never sent"})
        self.assertEqual(path.read_bytes(), original)


class StreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "audits"
        self.manager = AuditManager(self.root)
        self.addCleanup(self.manager.close)
        self.manager.start()
        self.activated = self.manager.activate("streams", "alpha")

    def test_stream_lifecycle_hash_and_complete_flag(self) -> None:
        call = _call(self.manager, "streams", self.activated["run_id"], "alpha")
        stream = self.manager.open_stream(
            call, role="stdout", channel="shell", operation_id="job-1", titella_id="alpha",
        )
        payload = b"hello" * 1000
        self.manager.append_stream(stream, payload[:2048])
        self.manager.append_stream(stream, payload[2048:], wire_seq=1)
        # duplicate identical wire_seq adds no bytes
        self.manager.append_stream(stream, payload[2048:], wire_seq=1)
        self.manager.close_stream(
            stream, complete=False, reason="link_disconnected",
            final_status={"exit_code": 7},
        )
        self.manager.flush_barrier()
        import hashlib
        data = (self.root / "streams" / "artifacts" / f"{stream}.bin").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), hashlib.sha256(payload).hexdigest())
        events = (self.root / "streams" / "events.jsonl").read_text()
        self.assertIn('"complete": false', events)
        self.assertIn('"reason": "link_disconnected"', events)

    def test_exit_code_preserved_verbatim(self) -> None:
        call = _call(self.manager, "streams", self.activated["run_id"], "alpha")
        stream = self.manager.open_stream(
            call, role="stdout", channel="shell", operation_id="job-2", titella_id="alpha",
        )
        self.manager.close_stream(
            stream, complete=True, reason="job.finished:exit7",
            final_status={"status": "finished", "exit_code": 7},
        )
        self.manager.flush_barrier()
        events = (self.root / "streams" / "events.jsonl").read_text()
        self.assertIn('"exit_code": 7', events)


if __name__ == "__main__":
    unittest.main()
