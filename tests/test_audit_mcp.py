"""Interceptor tests: audited tool calls through AuditFastMCP with fakes."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from titella.audit import AuditError
from titella.audit_mcp import AuditFastMCP
from titella.config import load_config
from titella.registry import TitellaInstance, TitellaRegistry

from tests.test_registry import _FakeLink, _fake_instance


class _FakeInstance:
    def __init__(self, titella_id: str) -> None:
        self.titella_id = titella_id


class _FakeRegistry:
    """Minimal resolve() surface the interceptor needs."""

    def __init__(self, instances: dict[str, _FakeInstance], audit: Any) -> None:
        self._instances = instances
        self.audit = audit

    def resolve(self, titella: str | None = None) -> _FakeInstance:
        if titella is None:
            if len(self._instances) == 1:
                return next(iter(self._instances.values()))
            raise LookupError("titella_required")
        instance = self._instances.get(str(titella))
        if instance is None:
            raise KeyError(str(titella))
        return instance


def _run(coro):
    return asyncio.run(coro)


class InterceptorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audit_root = self.root / "audits"
        from titella.audit import AuditManager

        self.manager = AuditManager(self.audit_root)
        self.addCleanup(self.manager.close)
        self.manager.start()
        self.reg = _FakeRegistry({"lab": _FakeInstance("lab")}, self.manager)
        self.server = AuditFastMCP("t", audit_registry=self.reg)
        self.sent = []

        @self.server.tool(description="dict tool")
        def remote_shell(cmd: str, titella: str | None = None) -> dict[str, Any]:
            self.sent.append((cmd, titella))
            return {"ok": True, "status": "finished", "stdout": "marker-out"}

        @self.server.tool(description="failing dict tool")
        def failing(titella: str | None = None) -> dict[str, Any]:
            return {"ok": False, "status": "error",
                    "error": {"code": "x", "message": "m", "retryable": False}}

        @self.server.tool(description="raising tool")
        def raising(titella: str | None = None) -> dict[str, Any]:
            raise RuntimeError("boom-dispatch")

        @self.server.tool(description="not intercepted")
        def docs_read(document_id: str) -> dict[str, Any]:
            return {"id": document_id}

        @self.server.tool(description="Safety action")
        def release_all(titella: str | None = None) -> dict[str, Any]:
            self.sent.append(("release_all", titella))
            return {"ok": True}

    def _events(self, audit_id: str) -> list[dict[str, Any]]:
        path = self.audit_root / audit_id / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def test_success_records_started_finished_and_full_arguments(self) -> None:
        self.manager.activate("itcpt", "lab")
        result = _run(self.server.call_tool(
            "remote_shell", {"cmd": "whoami --secret", "titella": "lab"}
        ))
        self.manager.flush_barrier()
        kinds = [e["kind"] for e in self._events("itcpt")]
        self.assertIn("tool.started", kinds)
        self.assertIn("tool.finished", kinds)
        self.assertNotIn("tool.failed", kinds)
        # full arguments kept as an input artifact, secrets included
        inputs = [
            a for a in (self.audit_root / "itcpt" / "artifacts").glob("*.json")
            if b"whoami --secret" in a.read_bytes()
        ]
        self.assertTrue(inputs)

    def test_structured_failure_classified_as_tool_failed(self) -> None:
        self.manager.activate("itcpt", "lab")
        _run(self.server.call_tool("failing", {"titella": "lab"}))
        self.manager.flush_barrier()
        kinds = [e["kind"] for e in self._events("itcpt")]
        self.assertIn("tool.failed", kinds)
        failed = next(e for e in self._events("itcpt") if e["kind"] == "tool.failed")
        self.assertEqual(failed["data"]["error"]["code"], "x")

    def test_dispatch_exception_recorded_and_reraised(self) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        self.manager.activate("itcpt", "lab")
        with self.assertRaises(ToolError):
            _run(self.server.call_tool("raising", {"titella": "lab"}))
        self.manager.flush_barrier()
        failed = [e for e in self._events("itcpt") if e["kind"] == "tool.failed"]
        self.assertTrue(failed)
        self.assertEqual(failed[0]["data"]["error"]["phase"], "dispatch")
        result_id = failed[0]["data"]["result_evidence_id"]
        result = json.loads((self.audit_root / "itcpt" / "artifacts" / f"{result_id}.json").read_text())
        self.assertIn("boom-dispatch", result["error"]["message"])

    def test_unknown_titella_records_nothing(self) -> None:
        # The interceptor never records a call into a foreign audit: the
        # tool itself answers titella_not_found, exactly as before.
        self.manager.activate("itcpt", "lab")
        _run(self.server.call_tool("remote_shell", {"cmd": "x", "titella": "ghost"}))
        self.manager.flush_barrier()
        kinds = [e["kind"] for e in self._events("itcpt")]
        self.assertNotIn("tool.started", kinds)
        self.assertNotIn("tool.finished", kinds)

    def test_non_operational_tool_not_intercepted(self) -> None:
        self.manager.activate("itcpt", "lab")
        _run(self.server.call_tool("docs_read", {"document_id": "op"}))
        self.manager.flush_barrier()
        kinds = [e["kind"] for e in self._events("itcpt")]
        self.assertEqual(kinds, ["audit.activated"])

    def test_missing_titella_argument_is_still_audited_and_pinned(self) -> None:
        from unittest.mock import patch
        self.manager.activate("defaulted", "lab")
        persist = self.manager.persist_call
        def change_default(call, arguments):
            persist(call, arguments)
            self.reg._instances["other"] = _FakeInstance("other")
        with patch.object(self.manager, "persist_call", side_effect=change_default):
            _run(self.server.call_tool("remote_shell", {"cmd": "once"}))
        self.assertEqual(self.sent, [("once", "lab")])
        self.assertIn("tool.finished", [event["kind"] for event in self._events("defaulted")])

    def test_storage_failure_before_dispatch_sends_nothing(self) -> None:
        import errno
        from unittest.mock import patch
        self.manager.activate("pre-failure", "lab")
        with patch.object(self.manager, "_artifact", side_effect=OSError(errno.ENOSPC, "disk full")):
            result = _run(self.server.call_tool("remote_shell", {"cmd": "never"}))
        content = result[0] if isinstance(result, tuple) else result
        rejected = json.loads(content[0].text)
        self.assertEqual(rejected["error"]["code"], "audit_recording_failed")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.manager.status("lab")["status"], "faulted")
        _run(self.server.call_tool("release_all", {}))
        self.assertEqual(self.sent, [("release_all", "lab")])

    def test_post_dispatch_failure_preserves_result_and_does_not_reexecute(self) -> None:
        import errno
        from unittest.mock import patch
        self.manager.activate("post-failure", "lab")
        artifact = self.manager._artifact
        def deny_result(call, role, *args, **kwargs):
            if role == "result":
                raise OSError(errno.EACCES, "access denied")
            return artifact(call, role, *args, **kwargs)
        with patch.object(self.manager, "_artifact", side_effect=deny_result):
            result = _run(self.server.call_tool("remote_shell", {"cmd": "once"}))
        content = result[0] if isinstance(result, tuple) else result
        self.assertEqual(json.loads(content[0].text)["stdout"], "marker-out")
        self.assertEqual(self.sent, [("once", "lab")])
        _run(self.server.call_tool("remote_shell", {"cmd": "blocked"}))
        self.assertEqual(self.sent, [("once", "lab")])
        self.assertEqual(self.manager.status("lab")["status"], "faulted")


class RegistryGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg_path = self.root / "config.toml"
        self.cfg_path.write_text(
            f'state_dir = "{self.root / "state"}"\n\n'
            '[titella.alpha]\nserial_port = "/dev/cu.alpha"\n\n'
            '[titella.beta]\nserial_port = "/dev/cu.beta"\n',
            encoding="utf-8",
        )

    def _registry(self) -> TitellaRegistry:
        import os

        config = load_config(
            self.cfg_path,
            {"TITELLA_AUDIT_ROOT": str(self.root / "audits")},
        )
        return TitellaRegistry(config, instance_factory=_fake_instance)

    def test_upsert_and_remove_reject_audited_instance_even_with_force(self) -> None:
        reg = self._registry()
        self.addCleanup(reg.close_all)
        reg.audit.activate("guard", "alpha")
        from titella.serial_link import TransportError

        with self.assertRaises(AuditError) as up:
            reg.upsert("alpha", serial_port="/dev/cu.other", force=True)
        self.assertEqual(up.exception.code, "audit_busy")
        with self.assertRaises(AuditError) as rm:
            reg.remove("alpha", force=True)
        self.assertEqual(rm.exception.code, "audit_busy")
        # after deactivation the guard lifts (the fake gate is ready, so
        # the transport change keeps needing force, as before auditing)
        reg.audit.deactivate("alpha")
        moved = reg.upsert("alpha", serial_port="/dev/cu.other", force=True)
        self.assertTrue(moved["ok"])

    def test_kvm_status_carries_audit_block_only_when_bound(self) -> None:
        reg = self._registry()
        self.addCleanup(reg.close_all)
        plain = reg.resolve("alpha").kvm_status()
        self.assertNotIn("audit", plain)
        reg.audit.activate("kvmaudit", "alpha")
        audited = reg.resolve("alpha").kvm_status()
        self.assertEqual(audited["audit"]["status"], "active")
        self.assertEqual(audited["audit"]["audit_id"], "kvmaudit")


if __name__ == "__main__":
    unittest.main()
