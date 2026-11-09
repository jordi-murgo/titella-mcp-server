"""Master state for one-shot jobs and remote PTY terminals."""
from __future__ import annotations

import base64
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .agent_state import AgentGate
from .serial_link import SerialLink, TransportError
from titella_slave_assets.wire import decode_data

RING_BYTES = 1024 * 1024
MAX_JOB_STREAM_BYTES = 1024 * 1024


class ByteRing:
    def __init__(self, limit: int = RING_BYTES) -> None:
        self.limit = limit
        self._chunks: deque[tuple[int, bytes]] = deque()
        self.start = 0
        self.end = 0

    def append(self, data: bytes) -> None:
        if not data:
            return
        self._chunks.append((self.end, data))
        self.end += len(data)
        while self.end - self.start > self.limit and self._chunks:
            offset, chunk = self._chunks[0]
            excess = self.end - self.start - self.limit
            if excess >= len(chunk):
                self._chunks.popleft()
                self.start = offset + len(chunk)
            else:
                self._chunks[0] = (offset + excess, chunk[excess:])
                self.start = offset + excess
                break

    def read(self, after: int, maximum: int) -> tuple[bytes, int | None, int]:
        dropped = self.start if after < self.start else None
        cursor = max(after, self.start)
        output = bytearray()
        for offset, chunk in self._chunks:
            chunk_end = offset + len(chunk)
            if chunk_end <= cursor:
                continue
            begin = max(0, cursor - offset)
            take = min(maximum - len(output), len(chunk) - begin)
            output.extend(chunk[begin:begin + take])
            cursor += take
            if len(output) >= maximum:
                break
        return bytes(output), dropped, cursor


@dataclass
class JobState:
    job_id: str
    started: float = field(default_factory=time.monotonic)
    status: str = "running"
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    result: dict[str, Any] = field(default_factory=dict)
    condition: threading.Condition = field(default_factory=threading.Condition)


@dataclass
class TerminalState:
    session_id: str
    ring: ByteRing = field(default_factory=ByteRing)
    write_seq: int = 0
    status: str = "running"
    condition: threading.Condition = field(default_factory=threading.Condition)


class ShellClient:
    def __init__(self, link: SerialLink, gate: AgentGate | None = None) -> None:
        self.link = link
        self.gate = gate or AgentGate()
        self.jobs: dict[str, JobState] = {}
        self.terminals: dict[str, TerminalState] = {}
        self._lock = threading.Lock()
        self._unsubscribe = link.subscribe("shell", self._on_frame)
        self._unsubscribe_state = link.subscribe_state(self._on_link_state)

    def close(self) -> None:
        self._unsubscribe()
        self._unsubscribe_state()

    def _on_link_state(self, state: str) -> None:
        if state not in {"reconnecting", "connected", "disconnected"}:
            return
        with self._lock:
            jobs = list(self.jobs.values())
            terminals = list(self.terminals.values())
        for job in jobs:
            with job.condition:
                if state == "reconnecting" and job.status == "running":
                    job.status = "reconnecting"
                elif state == "connected" and job.status == "reconnecting":
                    job.status = "running"
                elif state == "disconnected" and job.status in {"running", "reconnecting"}:
                    job.status = "disconnected"
                job.condition.notify_all()
        for terminal in terminals:
            with terminal.condition:
                if state == "reconnecting" and terminal.status == "running":
                    terminal.status = "reconnecting"
                elif state == "connected" and terminal.status == "reconnecting":
                    terminal.status = "running"
                elif state == "disconnected" and terminal.status in {"running", "reconnecting"}:
                    terminal.status = "disconnected"
                terminal.condition.notify_all()

    def _on_frame(self, frame: dict[str, Any]) -> bool:
        session_id = frame.get("session_id")
        if not isinstance(session_id, str):
            return True
        if frame["type"] == "data":
            data = decode_data(frame)
            terminal = self.terminals.get(session_id)
            if terminal is not None:
                with terminal.condition:
                    terminal.ring.append(data)
                    terminal.condition.notify_all()
                return True
            job = self.jobs.get(session_id)
            if job is not None:
                stream = frame.get("stream", "stdout")
                target = job.stderr if stream == "stderr" else job.stdout
                room = MAX_JOB_STREAM_BYTES - len(target)
                target.extend(data[:room])
                if len(data) > room:
                    if stream == "stderr":
                        job.stderr_truncated = True
                    else:
                        job.stdout_truncated = True
                return True
        if frame["type"] == "event":
            op = frame.get("op")
            body = dict(frame.get("body") or {})
            if op == "job.finished" and session_id in self.jobs:
                job = self.jobs[session_id]
                with job.condition:
                    job.status = str(body.get("status", "finished"))
                    job.result = body
                    job.condition.notify_all()
            elif op == "terminal.closed" and session_id in self.terminals:
                terminal = self.terminals[session_id]
                with terminal.condition:
                    terminal.status = "closed"
                    terminal.condition.notify_all()
        return True

    def remote_shell(self, cmd: str, sync_timeout: float = 10, timeout: float = 60, cwd: str | None = None) -> dict[str, Any]:
        self.gate.require()
        job_id = uuid.uuid4().hex
        job = JobState(job_id)
        with self._lock:
            self.jobs[job_id] = job
        response = self.link.request("shell", "job.start", {"job_id": job_id, "cmd": cmd, "timeout": timeout, "cwd": cwd})
        body = dict(response.get("body") or {})
        if body.get("status") == "error":
            with self._lock:
                self.jobs.pop(job_id, None)
            return body
        with job.condition:
            if job.status == "running" and sync_timeout > 0:
                job.condition.wait_for(lambda: job.status != "running", timeout=sync_timeout)
        if job.status == "running":
            return {"status": "running", "job_id": job_id}
        return self._job_result(job, inline=True)

    def _job_result(self, job: JobState, inline: bool = False) -> dict[str, Any]:
        result = {
            "status": job.status,
            "job_id": None if inline else job.job_id,
            "elapsed": time.monotonic() - job.started,
            "stdout": bytes(job.stdout).decode("utf-8", errors="replace"),
            "stderr": bytes(job.stderr).decode("utf-8", errors="replace"),
            "stdout_truncated": job.stdout_truncated,
            "stderr_truncated": job.stderr_truncated,
            **job.result,
        }
        if inline:
            result["job_id"] = None
        return result

    def shell_result(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            return {"status": "not_found", "job_id": job_id}
        if job.status == "running":
            return {"status": "running", "job_id": job_id, "elapsed": time.monotonic() - job.started}
        return self._job_result(job)

    def shell_cancel(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            return {"status": "not_found", "job_id": job_id}
        if job.status != "running":
            return self._job_result(job)
        response = self.link.request("shell", "job.cancel", {"job_id": job_id})
        return dict(response.get("body") or {})

    def terminal_open(self, cwd: str | None, shell: str | None, rows: int, cols: int, idle_timeout: int) -> dict[str, Any]:
        self.gate.require()
        response = self.link.request("shell", "terminal.open", {"cwd": cwd, "shell": shell, "rows": rows, "cols": cols, "idle_timeout": idle_timeout})
        body = dict(response.get("body") or {})
        session_id = body.get("session_id")
        if isinstance(session_id, str):
            self.terminals[session_id] = TerminalState(session_id)
        return body

    def terminal_read(self, session_id: str, after: int, wait_seconds: float, max_bytes: int, encoding: str) -> dict[str, Any]:
        terminal = self.terminals.get(session_id)
        if terminal is None:
            return {"status": "not_found", "session_id": session_id}
        maximum = min(max(1, max_bytes), 65536)
        with terminal.condition:
            if terminal.ring.end <= after and terminal.status == "running" and wait_seconds > 0:
                terminal.condition.wait(timeout=min(max(wait_seconds, 0), 30))
            data, dropped, next_offset = terminal.ring.read(after, maximum)
        result: dict[str, Any] = {"status": terminal.status, "session_id": session_id, "next_offset": next_offset}
        if dropped is not None:
            result["dropped_before"] = dropped
        if encoding == "base64":
            result["data"] = base64.b64encode(data).decode("ascii")
            result["encoding"] = "base64"
        else:
            result["data"] = data.decode(encoding, errors="replace")
            result["encoding"] = encoding
        return result

    def terminal_write(self, session_id: str, data: str, encoding: str) -> dict[str, Any]:
        terminal = self.terminals.get(session_id)
        if terminal is None:
            return {"status": "not_found", "session_id": session_id}
        raw = base64.b64decode(data, validate=True) if encoding == "base64" else data.encode(encoding)
        for position in range(0, len(raw), 512):
            self.link.send_data("shell", session_id, terminal.write_seq, raw[position:position + 512])
            terminal.write_seq += 1
        return {"status": "written", "session_id": session_id, "bytes": len(raw)}

    def terminal_resize(self, session_id: str, rows: int, cols: int) -> dict[str, Any]:
        response = self.link.request("shell", "terminal.resize", {"session_id": session_id, "rows": rows, "cols": cols})
        return dict(response.get("body") or {})

    def terminal_close(self, session_id: str) -> dict[str, Any]:
        terminal = self.terminals.get(session_id)
        if terminal is not None and terminal.status == "closed":
            return {"status": "closed", "session_id": session_id}
        response = self.link.request("shell", "terminal.close", {"session_id": session_id})
        return dict(response.get("body") or {})
