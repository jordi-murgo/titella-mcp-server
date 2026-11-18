"""Evidence round trip through the real SDK HTTP transport, without hardware."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from titella.config import load_config
from titella.mcp_server import create_server
from titella.registry import TitellaInstance, TitellaRegistry
from tests.test_registry import _FakeLink
from tests.test_screen_agent import make_png


class _AuditLink(_FakeLink):
    def emit(self, frame):
        for channel, callback, session in list(self.handlers):
            if channel == "shell" and (session is None or session == frame.get("session_id")):
                self.assert_callback(callback(frame))

    @staticmethod
    def assert_callback(value):
        if value is not True:
            raise AssertionError("audit subscriber must not suppress ACK")

    def request(self, channel, op, body=None, timeout=6):
        if channel == "shell" and op == "job.start":
            sid = body["job_id"]
            self.emit({"type": "data", "session_id": sid, "seq": 0, "stream": "stdout",
                       "data_b64": base64.b64encode(b"TRANSPORT-MARKER-7f3a\n").decode("ascii")})
            self.emit({"type": "event", "session_id": sid, "op": "job.finished",
                       "body": {"status": "finished", "exit_code": 7}})
            return {"body": {"status": "started"}}
        return super().request(channel, op, body, timeout)


def _instance(config):
    inst = TitellaInstance(config, link=_AuditLink(config))
    # Replace only the hardware grabber: real validation/cache/provenance run.
    inst.capture._uvc = lambda: inst.capture.record(make_png(32, 18), source="uvc")
    inst.gate.set_ready(True)
    return inst


def _text(result):
    return next(json.loads(item.text) for item in result.content if item.type == "text")


class _ReadyServer(uvicorn.Server):
    def __init__(self, config, ready):
        super().__init__(config)
        self.ready = ready

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        self.ready.set()


class AuditTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = load_config(self.root / "absent.toml", {
            "TITELLA_STATE_DIR": str(self.root / "state"),
            "TITELLA_AUDIT_ROOT": str(self.root / "audits"),
        })
        self.reg = TitellaRegistry(self.config, instance_factory=_instance)
        self.addCleanup(self.reg.close_all)
        server = create_server(self.config, registry=self.reg)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        ready = threading.Event()
        self.server = _ReadyServer(uvicorn.Config(server.streamable_http_app(), log_level="error", access_log=False), ready)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, name="test-audit-http")
        self.thread.start()
        self.addCleanup(self.stop)
        self.assertTrue(ready.wait(10), "HTTP server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)
        self.sock.close()
        self.assertFalse(self.thread.is_alive(), "HTTP server leaked after test")

    def test_two_clients_share_audit_and_export_exact_originals(self):
        async def scenario():
            async with streamable_http_client(self.url) as (r1, w1, _), streamable_http_client(self.url) as (r2, w2, _):
                async with ClientSession(r1, w1) as first, ClientSession(r2, w2) as second:
                    await first.initialize()
                    await second.initialize()
                    activated = _text(await first.call_tool("audit_activate", {"name": "transport-check"}))
                    self.assertTrue(activated["ok"], activated)
                    status = _text(await second.call_tool("audit_status", {}))
                    self.assertEqual(status["run_id"], activated["run_id"])
                    shell = _text(await second.call_tool("remote_shell", {"cmd": "authorized marker", "sync_timeout": 0}))
                    self.assertEqual(shell["exit_code"], 7)
                    self.assertIsNone(shell["job_id"])
                    screenshot = await first.call_tool("screen", {})
                    png = base64.b64decode(next(block.data for block in screenshot.content if block.type == "image"))
                    self.assertEqual(png, make_png(32, 18))
                    search = _text(await second.call_tool("audit_search", {"audit_id": "transport-check", "query": "TRANSPORT-MARKER-7f3a"}))
                    self.assertTrue(search["ok"], search)
                    stdout = next(hit["evidence_id"] for hit in search["hits"] if hit["role"] == "stdout")
                    chronology = _text(await first.call_tool("audit_search", {"audit_id": "transport-check", "kind": "capture.recorded"}))
                    capture = next(hit["evidence_id"] for hit in chronology["hits"] if hit.get("evidence_id"))
                    image_read = await second.call_tool("audit_read", {"audit_id": "transport-check", "evidence_id": capture})
                    self.assertEqual(base64.b64decode(next(block.data for block in image_read.content if block.type == "image")), png)
                    read = _text(await first.call_tool("audit_read", {"audit_id": "transport-check", "evidence_id": stdout}))
                    self.assertEqual(base64.b64decode(read["data_b64"]), b"TRANSPORT-MARKER-7f3a\n")
                    note = _text(await second.call_tool("audit_note", {"audit_id": "transport-check", "text": "Verified exit 7", "evidence_ids": [stdout, capture]}))
                    self.assertTrue(note["ok"], note)
                    inactive = _text(await first.call_tool("audit_deactivate", {}))
                    self.assertEqual(inactive["status"], "inactive", inactive)
                    exported = _text(await second.call_tool("audit_export", {"audit_id": "transport-check", "evidence_ids": [stdout, capture]}))
                    self.assertTrue(exported["ok"], exported)
                    directory = Path(exported["directory"])
                    manifest = json.loads((directory / "manifest.json").read_text())
                    self.assertTrue(manifest["contains_raw_evidence"])
                    self.assertEqual(set(manifest["selection"]), {stdout, capture})
                    for evidence in manifest["evidence"]:
                        payload = (directory / evidence["file"]).read_bytes()
                        self.assertEqual(hashlib.sha256(payload).hexdigest(), evidence["sha256"])
                    journal = self.root / "audits" / "transport-check" / "events.jsonl"
                    events = [json.loads(line) for line in journal.read_text().splitlines()]
                    self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
                    self.assertEqual(sum(event["kind"] == "capture.recorded" for event in events), 1)
        asyncio.run(scenario())

    def test_all_docs_resources_work_with_gate_closed(self):
        self.reg.resolve().gate.set_ready(False)
        async def scenario():
            async with streamable_http_client(self.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    index = _text(await session.call_tool("docs_index", {}))
                    ids = {entry["id"] for entry in index["documents"]}
                    self.assertLessEqual({"work-protocol", "audit-mode", "audit-report-skill"}, ids)
                    resources = {str(resource.uri) for resource in (await session.list_resources()).resources}
                    for entry in index["documents"]:
                        loaded = _text(await session.call_tool("docs_read", {"document_id": entry["id"]}))
                        self.assertTrue(loaded["content"].strip())
                        self.assertIn(entry["uri"], resources)
                        resource = await session.read_resource(entry["uri"])
                        self.assertEqual(resource.contents[0].text, loaded["content"])
                    self.assertEqual(_text(await session.call_tool("audit_status", {}))["status"], "inactive")
        asyncio.run(scenario())
