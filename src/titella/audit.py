"""Private evidence archives with one bounded writer and explicit recovery."""
from __future__ import annotations

import base64
import contextvars
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import __version__

AUDIT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
UUID_RE = re.compile(r"^[0-9a-f]{32}$")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_QUEUE_BYTES = 64 * 1024 * 1024
BARRIER_TIMEOUT_S = 5.0
MAX_NOTE_BYTES = 16384


def utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class AuditError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False, **context: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.retryable, self.context = code, message, retryable, context

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, **self.context}


def validate_audit_id(name: Any) -> str:
    if not isinstance(name, str) or not AUDIT_ID_RE.fullmatch(name):
        raise AuditError("invalid_argument", "audit_id must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    return name


def check_safe_descendant(root: Path, target: Path) -> Path:
    """Resolve only the authorized root, never a child that could hide a symlink."""
    if root.is_symlink():
        raise AuditError("audit_invalid_directory", "audit_root must not be a symlink")
    if ".." in target.parts:
        raise AuditError("audit_invalid_directory", "parent traversal is forbidden")
    original = Path(os.path.abspath(root.expanduser()))
    resolved = original.resolve()
    target = Path(os.path.abspath(target.expanduser()))
    try:
        relative = target.relative_to(original)
    except ValueError:
        try:
            relative = target.relative_to(resolved)
        except ValueError as exc:
            raise AuditError("audit_invalid_directory", "path is outside audit_root") from exc
    current = resolved
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise AuditError("audit_invalid_directory", "invalid relative path")
        current /= part
        if current.is_symlink():
            raise AuditError("audit_invalid_directory", f"symlink is forbidden: {current}")
    return current


def ensure_private_dir(path: Path, *, root: Path | None = None) -> None:
    path = check_safe_descendant(root, path) if root is not None else path
    if path.is_symlink():
        raise AuditError("audit_invalid_directory", f"symlink is forbidden: {path}")
    if not path.exists():
        if not path.parent.exists():
            ensure_private_dir(path.parent)
        path.mkdir(mode=0o700)
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode) or mode & 0o077:
        raise AuditError("audit_invalid_directory", f"directory is not private (0700): {path}")


def _private_file(path: Path) -> None:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise AuditError("audit_invalid_directory", f"file is not private (0600): {path}")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_private_bytes(root: Path, target: Path, data: bytes) -> None:
    target = check_safe_descendant(root, target)
    ensure_private_dir(target.parent, root=root)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
        _fsync_dir(target.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True)
class AuditCall:
    action_id: str | None
    audit_id: str
    run_id: str
    titella_id: str | None
    tool: str
    started_monotonic: float
    started_utc: str
    input_evidence_id: str | None = None
    request_metadata: Mapping[str, Any] = field(default_factory=dict)


_CURRENT_AUDIT_CALL: contextvars.ContextVar[AuditCall | None] = contextvars.ContextVar("audit_call", default=None)


def current_audit_call() -> AuditCall | None:
    return _CURRENT_AUDIT_CALL.get()


def set_current_audit_call(call: AuditCall | None) -> contextvars.Token:
    return _CURRENT_AUDIT_CALL.set(call)


def reset_current_audit_call(token: contextvars.Token) -> None:
    _CURRENT_AUDIT_CALL.reset(token)


@dataclass
class _AuditArchiveState:
    audit_id: str
    directory: Path
    created_at_utc: str
    last_seq: int = 0
    bytes_written: int = 0
    gaps_count: int = 0
    errors_count: int = 0
    corrupt_tail: bool = False
    corrupt_warning: str | None = None
    journal_size: int = 0


def inspect_events_file(events_path: Path) -> tuple[int, int, bool, str | None, list[dict[str, Any]]]:
    if events_path.is_symlink():
        raise AuditError("audit_invalid_directory", "journal must not be a symlink")
    if not events_path.exists():
        raise AuditError("audit_invalid_directory", "journal is missing")
    _private_file(events_path)
    events, total, seq = [], 0, 0
    with events_path.open("rb") as stream:
        for line in stream:
            try:
                if not line.endswith(b"\n"):
                    raise ValueError("incomplete final line")
                event = json.loads(line.decode("utf-8"))
                if not isinstance(event, dict) or event.get("schema_version") != 1:
                    raise ValueError("invalid event schema")
                if type(event.get("seq")) is not int or event["seq"] != seq + 1:
                    raise ValueError("non-contiguous event sequence")
                if not UUID_RE.fullmatch(str(event.get("event_id", ""))):
                    raise ValueError("invalid event id")
                if event.get("audit_id") != events_path.parent.name:
                    raise ValueError("event belongs to a different audit")
                if not isinstance(event.get("data"), dict):
                    raise ValueError("invalid event data")
                if not isinstance(event.get("kind"), str) or not isinstance(event.get("ts_utc"), str) or not event["ts_utc"].endswith("Z"):
                    raise ValueError("invalid event kind or UTC timestamp")
                datetime.fromisoformat(event["ts_utc"].replace("Z", "+00:00"))
                for key in ("run_id", "action_id"):
                    if key not in event or (event[key] is not None and not UUID_RE.fullmatch(str(event[key]))):
                        raise ValueError(f"invalid {key}")
                if "titella" not in event or not (event["titella"] is None or isinstance(event["titella"], str)):
                    raise ValueError("invalid instance metadata")
            except (ValueError, TypeError, UnicodeDecodeError) as exc:
                return seq, total, True, f"audit_corrupt: {exc}; only the valid prefix is available", events
            seq += 1
            total += len(line)
            events.append(event)
    return seq, total, False, None, events


def validate_or_init_audit_directory(root: Path, audit_id: str, *, create: bool) -> _AuditArchiveState:
    audit_id = validate_audit_id(audit_id)
    directory = check_safe_descendant(root, root / audit_id)
    if not directory.exists():
        if not create:
            raise AuditError("audit_not_found", f"audit {audit_id!r} does not exist")
        ensure_private_dir(root)
        ensure_private_dir(directory, root=root)
        ensure_private_dir(directory / "artifacts", root=root)
        manifest = {"schema_version": 1, "audit_id": audit_id, "created_at_utc": utc_now_rfc3339()}
        atomic_write_private_bytes(root, directory / "audit.json", _json_bytes(manifest))
        atomic_write_private_bytes(root, directory / "events.jsonl", b"")
    ensure_private_dir(directory, root=root)
    manifest_path = check_safe_descendant(root, directory / "audit.json")
    try:
        _private_file(manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "audit_id", "created_at_utc"}
                or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                or manifest["audit_id"] != audit_id or not isinstance(manifest["created_at_utc"], str)
                or not manifest["created_at_utc"].endswith("Z")):
            raise ValueError("invalid manifest")
        datetime.fromisoformat(manifest["created_at_utc"].replace("Z", "+00:00"))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AuditError("audit_invalid_directory", "directory has no valid version-1 audit manifest") from exc
    ensure_private_dir(directory / "artifacts", root=root)
    seq, size, corrupt, warning, events = inspect_events_file(directory / "events.jsonl")
    for path in (directory / "artifacts").iterdir():
        check_safe_descendant(root, path)
        _private_file(path)
    return _AuditArchiveState(audit_id, directory, manifest["created_at_utc"], seq, size,
                              sum(e["kind"] == "recording.gap" for e in events),
                              sum(e["kind"] == "tool.failed" for e in events), corrupt, warning, size)


def _load_active_bindings(root: Path) -> dict[str, dict[str, Any]]:
    path = check_safe_descendant(root, root / "active.json")
    if not path.exists():
        return {}
    try:
        _private_file(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or set(value) != {"schema_version", "bindings"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["bindings"], dict)):
            raise ValueError("invalid bindings schema")
        for tid, entry in value["bindings"].items():
            if not AUDIT_ID_RE.fullmatch(tid) or not isinstance(entry, dict) or set(entry) != {"audit_id", "run_id"}:
                raise ValueError("invalid binding")
            validate_audit_id(entry["audit_id"])
            if not isinstance(entry["run_id"], str) or not UUID_RE.fullmatch(entry["run_id"]):
                raise ValueError("invalid run id")
        return value["bindings"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AuditError("audit_invalid_directory", "active.json is corrupt; recording cannot be assumed inactive") from exc


@dataclass
class _BindingState:
    titella_id: str
    audit_id: str
    run_id: str
    status: str
    fault_error: dict[str, Any] | None = None
    gaps: int = 0
    gap_pending: bool = False


@dataclass
class _WriterItem:
    fn: Callable[[], Any]
    byte_cost: int
    audit_id: str | None = None
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


@dataclass
class _StreamState:
    call: AuditCall
    evidence_id: str
    role: str
    operation_id: str
    channel: str
    size: int = 0
    ordinal: int = 0
    hasher: Any = field(default_factory=hashlib.sha256)
    sealed: bool = False
    wire_hashes: dict[int, str] = field(default_factory=dict)


@dataclass
class _ShellSessionTracker:
    call: AuditCall
    kind: str
    action_id: str | None
    streams: dict[str, str] = field(default_factory=dict)
    seen: dict[int, str] = field(default_factory=dict)
    expected: int = 0
    anomaly: bool = False
    closed: bool = False


class AuditManager:
    """Only the writer thread touches evidence bytes; reader callbacks only enqueue."""

    def __init__(self, audit_root: Path, *, selection_lock: Any = None) -> None:
        if Path(audit_root).expanduser().is_symlink():
            raise AuditError("audit_invalid_directory", "audit_root must not be a symlink")
        self.root = Path(audit_root).expanduser().resolve()
        self._selection_lock = selection_lock or threading.RLock()
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._bindings: dict[str, _BindingState] = {}
        self._archives: dict[str, _AuditArchiveState] = {}
        self._open_calls: dict[str, AuditCall] = {}
        self._inflight: dict[str, int] = {}
        self._transitions: set[str] = set()
        self._open_operations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._shell_sessions: dict[tuple[str, str, str], _ShellSessionTracker] = {}
        self._streams: dict[str, _StreamState] = {}
        self._call_images: dict[str, list[tuple[str, str]]] = {}
        self._queue: deque[_WriterItem] = deque()
        self._queue_bytes = 0
        self._queue_cond = threading.Condition()
        self._writer_thread: threading.Thread | None = None
        self._writer_lock_fd: int | None = None
        self._shutdown = False
        self._started = False
        self._startup_error: AuditError | None = None
        self._writer_error: BaseException | None = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            if not self.root.exists():
                return
            try:
                ensure_private_dir(self.root)
                for tid, entry in _load_active_bindings(self.root).items():
                    self._bindings[tid] = _BindingState(tid, entry["audit_id"], entry["run_id"], "recovery_required")
            except AuditError as exc:
                self._startup_error = exc

    def enter_instance(self, tid: str) -> None:
        with self._selection_lock, self._lock:
            if tid in self._transitions:
                raise AuditError("audit_busy", "audit association is changing")
            self._inflight[tid] = self._inflight.get(tid, 0) + 1

    def exit_instance(self, tid: str) -> None:
        with self._lock:
            self._inflight[tid] = max(0, self._inflight.get(tid, 0) - 1)

    def pending(self, tid: str) -> list[str]:
        with self._lock:
            return ([f"calls:{self._inflight[tid]}"] if self._inflight.get(tid) else []) + [
                c.action_id for c in self._open_calls.values() if c.titella_id == tid
            ] + [key[2] for key in self._open_operations if key[1] == tid]

    def _ensure_writer(self) -> None:
        with self._lock:
            if self._shutdown:
                raise AuditError("audit_recording_failed", "audit writer is closed")
            if self._writer_thread is not None:
                return
            ensure_private_dir(self.root)
            path = check_safe_descendant(self.root, self.root / ".writer.lock")
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                _private_file(path)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, AuditError) as exc:
                os.close(fd)
                if isinstance(exc, AuditError):
                    raise
                raise AuditError("audit_root_busy", "another process owns the audit writer") from exc
            self._writer_lock_fd = fd
            self._writer_thread = threading.Thread(target=self._writer_loop, name="titella-audit-writer", daemon=True)
            self._writer_thread.start()

    def _fault(self, exc: BaseException, audit_id: str | None = None) -> None:
        with self._lock:
            self._writer_error = exc
            for binding in self._bindings.values():
                if (audit_id is None or binding.audit_id == audit_id) and binding.status != "recovery_required":
                    binding.status = "faulted"
                    binding.fault_error = AuditError("audit_recording_failed", str(exc)).as_dict()
                    binding.gaps += 1
                    binding.gap_pending = True

    def _enqueue(self, item: _WriterItem, *, byte_cost: int | None = None) -> None:
        cost = item.byte_cost if byte_cost is None else byte_cost
        failure = None
        with self._queue_cond:
            if self._shutdown:
                failure = AuditError("audit_recording_failed", "audit writer is closed")
            elif self._queue_bytes + cost > MAX_QUEUE_BYTES:
                failure = AuditError("audit_recording_failed", "audit queue exceeds 64 MiB")
            else:
                item.byte_cost = cost
                self._queue.append(item)
                self._queue_bytes += cost
                self._queue_cond.notify()
        if failure is not None:
            self._fault(failure, item.audit_id)
            raise failure

    def _wait_barrier(self, item: _WriterItem) -> Any:
        if not item.done.wait(BARRIER_TIMEOUT_S):
            error = AuditError("audit_recording_failed", "audit barrier exceeded five seconds")
            self._fault(error, item.audit_id)
            raise error
        if item.error is not None:
            if isinstance(item.error, AuditError):
                raise item.error
            raise AuditError("audit_recording_failed", str(item.error)) from item.error
        return item.result

    def _submit(self, fn: Callable[[], Any], cost: int = 256, *, audit_id: str | None = None, wait: bool = False) -> Any:
        item = _WriterItem(fn, cost, audit_id)
        self._enqueue(item)
        return self._wait_barrier(item) if wait else item

    def _writer_loop(self) -> None:
        while True:
            with self._queue_cond:
                self._queue_cond.wait_for(lambda: bool(self._queue) or self._shutdown)
                if not self._queue:
                    return
                item = self._queue.popleft()
            try:
                item.result = item.fn()
            except BaseException as exc:
                item.error = exc
                # Validation errors are not evidence loss. All storage/queue
                # failures fault the relevant association before the barrier.
                if not isinstance(exc, AuditError) or exc.code in {"audit_recording_failed", "audit_corrupt", "audit_invalid_directory"}:
                    self._fault(exc, item.audit_id)
            finally:
                with self._queue_cond:
                    self._queue_bytes -= item.byte_cost
                item.done.set()
            self._write_pending_gaps()

    def _write_pending_gaps(self) -> None:
        with self._lock:
            pending = [(b.audit_id, b.titella_id, b.run_id, b.fault_error) for b in self._bindings.values() if b.gap_pending]
        for aid, tid, run, error in pending:
            try:
                self._event(aid, run, tid, None, "recording.gap", {"error": error, "complete": False})
            except (OSError, AuditError):
                continue
            with self._lock:
                binding = self._bindings.get(tid)
                if binding and binding.run_id == run:
                    binding.gap_pending = False

    def flush_barrier(self) -> None:
        # Offline reads must not acquire a writer or create the root.
        if self._writer_thread is None:
            return
        self._submit(self._write_pending_gaps, wait=True)

    def _archive(self, aid: str, *, create: bool = False) -> _AuditArchiveState:
        if aid not in self._archives:
            self._archives[aid] = validate_or_init_audit_directory(self.root, aid, create=create)
        return self._archives[aid]

    def _event(self, aid: str, run: str | None, tid: str | None, action: str | None, kind: str, data: dict) -> dict:
        archive = self._archive(aid)
        if archive.corrupt_tail:
            raise AuditError("audit_corrupt", archive.corrupt_warning or "journal is corrupt")
        event = {"schema_version": 1, "audit_id": aid, "run_id": run, "titella": tid,
                 "action_id": action, "event_id": uuid.uuid4().hex, "seq": archive.last_seq + 1,
                 "ts_utc": utc_now_rfc3339(), "kind": kind, "data": data}
        data_bytes = _json_bytes(event)
        path = check_safe_descendant(self.root, archive.directory / "events.jsonl")
        if path.stat().st_size != archive.journal_size:
            archive.corrupt_tail = True
            archive.corrupt_warning = "audit_corrupt: journal size changed outside the writer; originals retained"
            raise AuditError("audit_corrupt", archive.corrupt_warning)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            view = memoryview(data_bytes)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("journal write made no progress")
                view = view[written:]
            os.fsync(fd)
        except OSError:
            _, valid_bytes, corrupt, warning, _ = inspect_events_file(path)
            archive.corrupt_tail = corrupt or valid_bytes != archive.journal_size
            archive.corrupt_warning = warning or "journal append failed; original tail retained"
            raise
        finally:
            os.close(fd)
        archive.last_seq = event["seq"]
        archive.bytes_written += len(data_bytes)
        archive.journal_size += len(data_bytes)
        archive.gaps_count += kind == "recording.gap"
        archive.errors_count += kind == "tool.failed"
        return event

    def _persist_bindings(self) -> None:
        with self._lock:
            value = {"schema_version": 1, "bindings": {tid: {"audit_id": b.audit_id, "run_id": b.run_id}
                     for tid, b in self._bindings.items()}}
        atomic_write_private_bytes(self.root, self.root / "active.json", _json_bytes(value))

    def activate(self, name: str, titella_id: str, busy_ids: Callable[[str], list[str]] | None = None) -> dict:
        aid, tid = validate_audit_id(name), titella_id
        self.start()
        with self._lifecycle:
            with self._selection_lock, self._lock:
                if hasattr(self, "_instance_exists") and not self._instance_exists(tid):
                    raise AuditError("titella_not_found", "the selected instance is no longer registered")
                if self._startup_error:
                    raise self._startup_error
                old = self._bindings.get(tid)
                if old and old.status == "faulted":
                    raise AuditError("audit_recording_failed", "recording failed; deactivate before changing the association")
                if old and old.status == "active":
                    if old.audit_id != aid:
                        raise AuditError("audit_already_active", "deactivate the current audit first")
                    return self._activation_result(old)
                if old and old.audit_id != aid:
                    raise AuditError("audit_recovery_required", "explicitly resume the previous audit or deactivate it")
                pending = self.pending(tid) + (busy_ids(tid) if busy_ids else [])
                if pending:
                    raise AuditError("audit_busy", "finish or cancel pending operations first", pending=pending)
                self._transitions.add(tid)
            try:
                self._ensure_writer()
                run = uuid.uuid4().hex
                def activate_on_writer() -> None:
                    archive = validate_or_init_audit_directory(self.root, aid, create=True)
                    if archive.corrupt_tail:
                        raise AuditError("audit_corrupt", archive.corrupt_warning or "journal is corrupt")
                    self._archives[aid] = archive
                    if old:
                        self._recover(old)
                    # Persist association before recording activation: a crash
                    # from this point still requires explicit recovery.
                    with self._lock:
                        self._bindings[tid] = _BindingState(tid, aid, run, "active")
                    self._persist_bindings()
                    from titella_slave_assets import __version__ as assets_version
                    self._event(aid, run, tid, None, "audit.activated",
                                {"resumed": old is not None, "server_version": __version__, "slave_assets_version": assets_version})
                self._submit(activate_on_writer, audit_id=aid, wait=True)
                return self._activation_result(self._bindings[tid])
            finally:
                with self._selection_lock, self._lock:
                    self._transitions.discard(tid)

    def _activation_result(self, binding: _BindingState) -> dict:
        return {"ok": True, "status": "active", "audit_id": binding.audit_id, "titella": binding.titella_id,
                "directory": str(self.root / binding.audit_id), "run_id": binding.run_id}

    def _recover(self, old: _BindingState) -> None:
        events = inspect_events_file(self.root / old.audit_id / "events.jsonl")[4]
        self._event(old.audit_id, old.run_id, old.titella_id, None, "daemon.interrupted", {"previous_run_id": old.run_id})
        operations: dict[str, dict] = {}
        calls: dict[str, dict] = {}
        streams: dict[str, dict] = {}
        for event in events:
            if event["run_id"] != old.run_id or event["titella"] != old.titella_id:
                continue
            data = event["data"]
            operation = data.get("operation_id")
            if event["kind"] == "operation.started" and operation:
                operations[operation] = {**data, "action_id": event["action_id"]}
            elif event["kind"] in {"operation.finished", "operation.interrupted"}:
                operations.pop(operation, None)
            elif event["kind"] == "tool.started":
                calls[event["action_id"]] = data
            elif event["kind"] in {"tool.finished", "tool.failed"}:
                calls.pop(event["action_id"], None)
            eid = data.get("evidence_id")
            if event["kind"] == "evidence.created" and data.get("sealed") is False:
                streams[eid] = {**data, "action_id": event["action_id"]}
            elif event["kind"] == "stream.chunk" and eid in streams:
                streams[eid]["size"] = data["offset"] + data["length"]
            elif event["kind"] == "stream.closed":
                streams.pop(eid, None)
        for operation, data in operations.items():
            self._event(old.audit_id, old.run_id, old.titella_id, data.get("action_id"), "operation.interrupted",
                        {"operation_id": operation, "channel": data.get("channel"), "status": "unknown", "result": "unknown_after_restart", "complete": False})
        for action in calls:
            self._event(old.audit_id, old.run_id, old.titella_id, action, "operation.interrupted",
                        {"action_id": action, "status": "unknown", "result": "unknown_after_restart", "complete": False})
        self._recover_streams(old, streams)

    def _recover_streams(self, old: _BindingState, streams: dict[str, dict]) -> None:
        for eid, metadata in streams.items():
            if not isinstance(eid, str) or not UUID_RE.fullmatch(eid):
                raise AuditError("audit_corrupt", "invalid stream identity in prior run")
            path = check_safe_descendant(self.root, self.root / old.audit_id / "artifacts" / f"{eid}.bin")
            confirmed = metadata.get("size", 0)
            if not path.is_file() or path.stat().st_size != confirmed:
                self._event(old.audit_id, old.run_id, old.titella_id, metadata.get("action_id"), "recording.gap",
                            {"evidence_id": eid, "reason": "unconfirmed bytes after interruption", "complete": False})
                continue
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(65536):
                    hasher.update(block)
            self._event(old.audit_id, old.run_id, old.titella_id, metadata.get("action_id"), "stream.closed",
                        {"evidence_id": eid, "size": confirmed, "sha256": hasher.hexdigest(), "sealed": True,
                         "complete": False, "reason": "daemon_interrupted"})

    def deactivate(self, titella_id: str, busy_ids: Callable[[str], list[str]] | None = None) -> dict:
        tid = titella_id
        with self._lifecycle:
            with self._selection_lock, self._lock:
                old = self._bindings.get(tid)
                if not old:
                    return {"ok": True, "status": "inactive", "audit_id": None, "titella": tid}
                pending = self.pending(tid) + (busy_ids(tid) if busy_ids else [])
                if pending:
                    raise AuditError("audit_busy", "finish or cancel pending operations first", pending=pending)
                self._transitions.add(tid)
            warnings = []
            try:
                self._ensure_writer()
                def deactivate_on_writer() -> None:
                    archive = validate_or_init_audit_directory(self.root, old.audit_id, create=False)
                    self._archives[old.audit_id] = archive
                    if archive.corrupt_tail:
                        warnings.append(archive.corrupt_warning or "deactivation could not be journaled: audit_corrupt")
                    else:
                        if old.status == "recovery_required":
                            self._recover(old)
                        self._event(old.audit_id, old.run_id, tid, None, "audit.deactivated", {"previous_status": old.status})
                    with self._lock:
                        self._bindings.pop(tid, None)
                    try:
                        self._persist_bindings()
                    except BaseException:
                        with self._lock:
                            self._bindings[tid] = old
                        raise
                self._submit(deactivate_on_writer, audit_id=old.audit_id, wait=True)
            finally:
                with self._selection_lock, self._lock:
                    self._transitions.discard(tid)
            return {"ok": True, "status": "inactive", "audit_id": old.audit_id, "titella": tid, "warnings": warnings}

    def status(self, titella_id: str | None = None) -> dict:
        self.start()
        with self._lock:
            if self._startup_error:
                return {"ok": False, "status": "faulted", "error": self._startup_error.as_dict(), "pending": []}
            binding = self._bindings.get(titella_id) if titella_id else None
            if not binding:
                return {"ok": True, "status": "inactive", "titella": titella_id, "pending": self.pending(titella_id) if titella_id else [],
                        "bindings": {tid: {"audit_id": b.audit_id, "run_id": b.run_id, "status": b.status} for tid, b in self._bindings.items()}}
            archive = self._archives.get(binding.audit_id)
            return {"ok": True, "status": binding.status, "audit_id": binding.audit_id, "run_id": binding.run_id,
                    "titella": titella_id, "directory": str(self.root / binding.audit_id), "pending": self.pending(titella_id),
                    "last_seq": archive.last_seq if archive else None, "bytes": archive.bytes_written if archive else 0,
                    "gaps": max(binding.gaps, archive.gaps_count if archive else 0), "errors": archive.errors_count if archive else 0,
                    "fault": binding.fault_error}

    def list_audits(self) -> dict:
        self.start()
        audits = []
        if self.root.exists():
            ensure_private_dir(self.root)
            for path in self.root.iterdir():
                if not AUDIT_ID_RE.fullmatch(path.name) or not path.is_dir():
                    continue
                archive = validate_or_init_audit_directory(self.root, path.name, create=False)
                audits.append({"audit_id": path.name, "directory": str(archive.directory), "created_at_utc": archive.created_at_utc,
                               "active_titellas": [tid for tid, b in self._bindings.items() if b.audit_id == path.name and b.status == "active"],
                               "last_seq": archive.last_seq})
        return {"ok": True, "audits": sorted(audits, key=lambda a: (a["created_at_utc"], a["audit_id"])), "audit_root": str(self.root)}

    def reserve_call(self, tid: str, tool: str, metadata: Mapping | None = None) -> AuditCall | None:
        self.start()
        with self._lock:
            if self._startup_error:
                raise self._startup_error
            binding = self._bindings.get(tid)
            if not binding:
                return None
            if binding.status != "active":
                code = "audit_recording_failed" if binding.status == "faulted" else "audit_recovery_required"
                raise AuditError(code, "explicit audit recovery or deactivation is required before new actions")
            if tid in self._transitions:
                raise AuditError("audit_busy", "audit association is changing")
            call = AuditCall(uuid.uuid4().hex, binding.audit_id, binding.run_id, tid, tool, time.monotonic(), utc_now_rfc3339(),
                             uuid.uuid4().hex, dict(metadata or {}))
            self._open_calls[call.action_id] = call
            return call

    def persist_call(self, call: AuditCall, arguments: Mapping) -> None:
        try:
            payload = _json_bytes({"role": "input", "arguments": dict(arguments), "tool": call.tool, "titella": call.titella_id})
            def write() -> None:
                self._artifact(call, "input", payload, "application/json", "json", call.input_evidence_id)
                self._event(call.audit_id, call.run_id, call.titella_id, call.action_id, "tool.started",
                            {"tool": call.tool, "input_evidence_id": call.input_evidence_id, "request": dict(call.request_metadata)})
            self._submit(write, len(payload) + 512, audit_id=call.audit_id, wait=True)
            with self._lock:
                binding = self._bindings.get(call.titella_id)
                if binding is None or binding.run_id != call.run_id or binding.status != "active":
                    raise AuditError("audit_recording_failed", "audit stopped accepting calls before dispatch")
        except BaseException:
            with self._lock:
                self._open_calls.pop(call.action_id, None)
            raise

    def begin_call(self, titella_id: str, tool: str, arguments: Mapping, request_metadata: Mapping | None = None) -> AuditCall | None:
        with self._selection_lock:
            call = self.reserve_call(titella_id, tool, request_metadata)
        if call:
            self.persist_call(call, arguments)
        return call

    def finish_call(self, call: AuditCall, result: Any = None, error: Any = None) -> None:
        try:
            # Serialization happens outside transport callbacks and preserves
            # both SDK content blocks and structured output.
            serialized = self._serialize_result(call, result)
            payload = _json_bytes({"role": "result", "tool": call.tool, "result": serialized, "error": error})
            def write() -> None:
                eid = self._artifact(call, "result", payload, "application/json", "json")
                data = {"tool": call.tool, "result_evidence_id": eid, "duration_s": max(0, time.monotonic() - call.started_monotonic)}
                if error:
                    data["error"] = {key: error[key] for key in ("code", "retryable", "phase") if key in error}
                self._event(call.audit_id, call.run_id, call.titella_id, call.action_id,
                            "tool.failed" if error else "tool.finished", data)
            self._submit(write, len(payload) + 512, audit_id=call.audit_id, wait=True)
        except BaseException as exc:
            self._fault(exc, call.audit_id)
            raise
        finally:
            with self._lock:
                self._open_calls.pop(call.action_id, None)
                self._call_images.pop(call.action_id, None)

    def _serialize_result(self, call: AuditCall, value: Any) -> Any:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            if value.get("type") == "image" and value.get("mimeType") == "image/png":
                image = base64.b64decode(value["data"], validate=True)
                digest = hashlib.sha256(image).hexdigest()
                with self._lock:
                    candidates = self._call_images.get(call.action_id, [])
                    matching = next((entry for entry in candidates if entry[1] == digest), None)
                    if matching:
                        candidates.remove(matching)
                eid = matching[0] if matching else self.record_artifact(call, role="capture", data=image, mime_type="image/png", extension="png")
                return {"type": "image", "mimeType": "image/png", "evidence_id": eid, "sha256": digest}
            return {str(k): self._serialize_result(call, v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [self._serialize_result(call, v) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, bytes):
            return {"data_b64": base64.b64encode(value).decode("ascii")}
        raise TypeError(f"unsupported evidence value: {type(value).__name__}")

    def _artifact(self, call: AuditCall, role: str, data: bytes, mime: str, extension: str,
                  eid: str | None = None, metadata: Mapping | None = None, related: list | None = None) -> str:
        if extension not in {"json", "png", "bin"}:
            raise AuditError("invalid_argument", "invalid artifact extension")
        eid = eid or uuid.uuid4().hex
        relative = f"artifacts/{eid}.{extension}"
        archive = self._archive(call.audit_id)
        atomic_write_private_bytes(self.root, archive.directory / relative, data)
        digest = (metadata or {}).get("artifact_sha256") or hashlib.sha256(data).hexdigest()
        self._event(call.audit_id, call.run_id, call.titella_id, call.action_id, "evidence.created",
                    {**dict(metadata or {}), "evidence_id": eid, "relative_path": relative, "mime_type": mime, "role": role,
                     "size": len(data), "sha256": digest, "sealed": True, "complete": True,
                     "action_id": call.action_id, "related_evidence_ids": list(related or [])})
        archive.bytes_written += len(data)
        return eid

    def record_artifact(self, call: AuditCall | None, *, role: str, data: bytes, mime_type: str, extension: str,
                        metadata: dict | None = None, related_evidence_ids: list | None = None, titella_id: str | None = None) -> str | None:
        if not call:
            return None
        eid = uuid.uuid4().hex
        self._submit(lambda: self._artifact(call, role, data, mime_type, extension, eid, metadata, related_evidence_ids),
                     len(data) + 512, audit_id=call.audit_id, wait=True)
        return eid

    def bind_operation(self, call: AuditCall | None, channel: str, operation_id: str, operation_kind: str) -> None:
        if not call:
            return
        key = (call.run_id, call.titella_id, operation_id)
        with self._lock:
            if key in self._open_operations:
                return
            data = {"channel": channel, "operation_id": operation_id, "operation_kind": operation_kind, "action_id": call.action_id}
            self._open_operations[key] = data
        self._submit(lambda: self._event(call.audit_id, call.run_id, call.titella_id, call.action_id, "operation.started", data), audit_id=call.audit_id)

    def finish_operation(self, call: AuditCall | None, channel: str, operation_id: str, outcome: dict) -> None:
        if not call:
            return
        self._submit(lambda: self._finish_operation(call, channel, operation_id, outcome), audit_id=call.audit_id)

    def operation_progress(self, call: AuditCall | None, channel: str, operation_id: str, metadata: dict) -> None:
        if call is None:
            return
        self._submit(lambda: self._event(call.audit_id, call.run_id, call.titella_id, call.action_id, "operation.bound",
                     {"channel": channel, "operation_id": operation_id, "action_id": call.action_id,
                      "observation": "progress", **metadata}), len(_json_bytes(metadata)) + 256, audit_id=call.audit_id)

    def _finish_operation(self, call: AuditCall, channel: str, operation_id: str, outcome: dict, interrupted: bool = False) -> None:
        key = (call.run_id, call.titella_id, operation_id)
        with self._lock:
            data = self._open_operations.pop(key, None)
        if data:
            self._event(call.audit_id, call.run_id, call.titella_id, data.get("action_id"),
                        "operation.interrupted" if interrupted else "operation.finished",
                        {**data, **outcome, "channel": channel, "operation_id": operation_id})

    def open_stream(self, call: AuditCall | None, *, role: str, channel: str, operation_id: str, titella_id: str) -> str | None:
        if not call:
            return None
        eid = uuid.uuid4().hex
        stream = _StreamState(call, eid, role, operation_id, channel)
        with self._lock:
            self._streams[eid] = stream
        try:
            self._submit(lambda: self._create_stream(stream), audit_id=call.audit_id)
        except AuditError:
            with self._lock:
                self._streams.pop(eid, None)
            raise
        return eid

    def _create_stream(self, stream: _StreamState) -> None:
        call = stream.call
        archive = self._archive(call.audit_id)
        relative = f"artifacts/{stream.evidence_id}.bin"
        path = check_safe_descendant(self.root, archive.directory / relative)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(path.parent)
        self._event(call.audit_id, call.run_id, call.titella_id, call.action_id, "evidence.created",
                    {"evidence_id": stream.evidence_id, "relative_path": relative, "mime_type": "application/octet-stream",
                     "role": stream.role, "size": 0, "sha256": EMPTY_SHA256, "sealed": False, "complete": False,
                     "action_id": call.action_id, "related_evidence_ids": [], "operation_id": stream.operation_id, "channel": stream.channel})

    def append_stream(self, evidence_id: str, data: bytes, *, wire_seq: int | None = None) -> bool:
        with self._lock:
            stream = self._streams.get(evidence_id)
        if not stream:
            return False
        try:
            self._submit(lambda: self._append_stream(stream, data, wire_seq), len(data) + 256, audit_id=stream.call.audit_id)
            return True
        except AuditError:
            return False

    def _append_stream(self, stream: _StreamState, data: bytes, wire_seq: int | None) -> None:
        if stream.sealed:
            self._gap(stream.call, "bytes received after stream closure")
            return
        digest = hashlib.sha256(data).hexdigest()
        if wire_seq is not None:
            previous = stream.wire_hashes.get(wire_seq)
            if previous is not None:
                if previous != digest:
                    self._gap(stream.call, "duplicate stream sequence has different bytes")
                return
            stream.wire_hashes[wire_seq] = digest
        path = check_safe_descendant(self.root, self.root / stream.call.audit_id / "artifacts" / f"{stream.evidence_id}.bin")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("stream write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        offset = stream.size
        stream.size += len(data)
        stream.hasher.update(data)
        self._event(stream.call.audit_id, stream.call.run_id, stream.call.titella_id, stream.call.action_id, "stream.chunk",
                    {"evidence_id": stream.evidence_id, "offset": offset, "length": len(data), "ordinal": stream.ordinal,
                     "wire_seq": wire_seq, "chunk_sha256": digest, "confirmed_prefix": stream.size,
                     "prefix_sha256": stream.hasher.hexdigest()})
        stream.ordinal += 1

    def close_stream(self, evidence_id: str, *, complete: bool, reason: str, final_status: dict | None = None) -> None:
        with self._lock:
            stream = self._streams.get(evidence_id)
        if stream:
            self._submit(lambda: self._close_stream(stream, complete, reason, final_status), audit_id=stream.call.audit_id)

    def _close_stream(self, stream: _StreamState, complete: bool, reason: str, status: dict | None = None) -> None:
        if stream.sealed:
            return
        stream.sealed = True
        binding = self._bindings.get(stream.call.titella_id)
        complete = complete and not (binding and (binding.gaps or binding.status == "faulted"))
        self._event(stream.call.audit_id, stream.call.run_id, stream.call.titella_id, stream.call.action_id, "stream.closed",
                    {"evidence_id": stream.evidence_id, "size": stream.size, "sha256": stream.hasher.hexdigest(),
                     "sealed": True, "complete": bool(complete), "reason": reason, "status": status})

    def register_shell_session(self, call: AuditCall | None, *, channel: str, session_id: str, kind: str, titella_id: str) -> None:
        if not call:
            return
        with self._lock:
            key = (call.run_id, titella_id, session_id)
            tracker = self._shell_sessions.get(key)
            if tracker is None:
                self._shell_sessions[key] = _ShellSessionTracker(call, kind, call.action_id)
            elif tracker.action_id is None:
                tracker.call, tracker.kind, tracker.action_id = call, kind, call.action_id

    def bind_shell_session(self, call: AuditCall, *, session_id: str, action_id: str) -> None:
        def bind() -> None:
            key = (call.run_id, call.titella_id, session_id)
            with self._lock:
                tracker = self._shell_sessions.get(key)
                if tracker:
                    tracker.call, tracker.action_id, tracker.kind = call, action_id, "terminal"
            self._event(call.audit_id, call.run_id, call.titella_id, action_id, "operation.bound",
                        {"session_id": session_id, "operation_id": session_id, "channel": "shell", "action_id": action_id,
                         "evidence_ids": list(tracker.streams.values()) if tracker else []})
        self._submit(bind, audit_id=call.audit_id)

    def record_shell_frame(self, titella_id: str, frame: dict) -> bool:
        binding = None
        try:
            with self._lock:
                binding = self._bindings.get(titella_id)
                if not binding or binding.status != "active":
                    return True
                call = AuditCall(None, binding.audit_id, binding.run_id, titella_id, "shell.stream", time.monotonic(), utc_now_rfc3339())
            # Do not decode/hash/write/wait on the transport's reader thread.
            payload = dict(frame)
            size = len(payload.get("data_b64", "")) + len(json.dumps(payload.get("body", {}))) + 512
            self._submit(lambda: self._shell_frame(call, payload), size, audit_id=call.audit_id)
        except BaseException as exc:
            self._fault(exc, binding.audit_id if binding else None)
        return True

    def _shell_frame(self, identity: AuditCall, frame: dict) -> None:
        sid = frame.get("session_id")
        if not isinstance(sid, str):
            return
        key = (identity.run_id, identity.titella_id, sid)
        with self._lock:
            tracker = self._shell_sessions.get(key)
            if not tracker:
                tracker = _ShellSessionTracker(identity, "unresolved_terminal", None)
                self._shell_sessions[key] = tracker
        if frame.get("type") == "data":
            from titella_slave_assets.wire import decode_data
            data = decode_data(frame)
            seq = frame.get("seq")
            digest = hashlib.sha256(data).hexdigest() + ":" + str(frame.get("stream"))
            if type(seq) is not int or seq < 0:
                self._gap(identity, "shell frame has invalid sequence")
                tracker.anomaly = True
            elif seq in tracker.seen:
                if tracker.seen[seq] != digest:
                    self._gap(identity, "duplicate shell sequence has different content")
                    tracker.anomaly = True
                return
            else:
                tracker.seen[seq] = digest
                if seq != tracker.expected:
                    tracker.anomaly = True
                    self._gap(identity, "shell frames arrived out of order or with a sequence gap")
                tracker.expected = seq + 1
            if tracker.closed:
                self._gap(identity, "shell bytes arrived after remote finish")
                return
            role = "terminal_output" if tracker.kind in {"terminal", "unresolved_terminal"} else ("stderr" if frame.get("stream") == "stderr" else "stdout")
            if role not in tracker.streams:
                eid = uuid.uuid4().hex
                stream = _StreamState(tracker.call, eid, role, sid, "shell")
                self._streams[eid] = stream
                tracker.streams[role] = eid
                self._create_stream(stream)
            self._append_stream(self._streams[tracker.streams[role]], data, seq if type(seq) is int else None)
        elif frame.get("type") == "event" and frame.get("op") in {"job.finished", "terminal.closed"}:
            body = dict(frame.get("body") or {})
            tracker.closed = True
            for eid in tracker.streams.values():
                self._close_stream(self._streams[eid], not tracker.anomaly, str(frame["op"]), body)
            self._finish_operation(tracker.call, "shell", sid, body)

    def _gap(self, call: AuditCall, reason: str) -> None:
        with self._lock:
            binding = self._bindings.get(call.titella_id)
            if binding:
                binding.gaps += 1
        self._event(call.audit_id, call.run_id, call.titella_id, call.action_id, "recording.gap", {"reason": reason, "complete": False})

    def record_gap(self, titella_id: str, reason: str) -> None:
        with self._lock:
            binding = self._bindings.get(titella_id)
            if not binding:
                return
            call = AuditCall(uuid.uuid4().hex, binding.audit_id, binding.run_id, titella_id, "recording", time.monotonic(), utc_now_rfc3339())
        self._submit(lambda: self._gap(call, reason), audit_id=call.audit_id)

    def note_link_state(self, titella_id: str, state: str) -> None:
        if state not in {"agent_unreachable", "disconnected", "closed"}:
            return
        with self._lock:
            binding = self._bindings.get(titella_id)
            if not binding:
                return
            aid, run = binding.audit_id, binding.run_id
        def interrupt() -> None:
            for key, tracker in list(self._shell_sessions.items()):
                if key[:2] != (run, titella_id) or tracker.closed:
                    continue
                tracker.closed = True
                self._gap(tracker.call, f"link {state}; remote outcome unknown")
                for eid in tracker.streams.values():
                    self._close_stream(self._streams[eid], False, f"link_{state}")
                self._finish_operation(tracker.call, "shell", key[2], {"status": "unknown", "complete": False, "reason": state}, True)
        try:
            self._submit(interrupt, audit_id=aid)
        except AuditError:
            pass

    def record_note(self, audit_id: str, text: str, evidence_ids: list[str]) -> str:
        self._ensure_writer()
        call = AuditCall(None, audit_id, uuid.uuid4().hex, None, "audit_note", time.monotonic(), utc_now_rfc3339())
        def write() -> str:
            self._archive(audit_id)
            eid = self._artifact(call, "note", _json_bytes({"text": text}), "application/json", "json", related=evidence_ids)
            self._event(audit_id, call.run_id, None, None, "note", {"evidence_id": eid, "related_evidence_ids": evidence_ids})
            return eid
        return self._submit(write, len(text.encode("utf-8")) + 512, audit_id=audit_id, wait=True)

    def record_export(self, audit_id: str, export_id: str, evidence_ids: list[str], directory: str) -> None:
        self._ensure_writer()
        self._submit(lambda: self._event(audit_id, None, None, None, "export.created",
                     {"export_id": export_id, "evidence_ids": evidence_ids, "contains_raw_evidence": True}), audit_id=audit_id, wait=True)

    def record_ocr(self, titella_id: str, *, source_evidence_id: str | None, frame_sha256: str | None,
                   image_bytes: bytes, text: str, scope: str, bbox: Any, model: str | None) -> str | None:
        call = current_audit_call()
        if not call:
            sink = _CaptureEvidenceSink(self, titella_id)
            call = sink._identity()
        if not call:
            return None
        def write() -> str:
            image = self._artifact(call, "ocr_image", image_bytes, "image/png", "png", related=[source_evidence_id] if source_evidence_id else [])
            result = self._artifact(call, "result", _json_bytes({"text": text, "scope": scope, "bbox_px": bbox, "model": model}),
                                    "application/json", "json", related=[image])
            self._event(call.audit_id, call.run_id, titella_id, call.action_id, "ocr.recorded",
                        {"source_evidence_id": source_evidence_id, "frame_sha256": frame_sha256, "image_evidence_id": image,
                         "result_evidence_id": result, "scope": scope, "bbox_px": bbox, "model": model})
            return image
        return self._submit(write, len(image_bytes) + len(text.encode("utf-8")) + 1024, audit_id=call.audit_id, wait=True)

    def faulted(self, titella_id: str) -> dict | None:
        with self._lock:
            binding = self._bindings.get(titella_id)
            return binding.fault_error if binding and binding.status == "faulted" else None

    def close(self) -> None:
        if self._shutdown:
            return
        for tid in list(self._bindings):
            self.note_link_state(tid, "closed")
        if self._writer_thread is not None:
            try:
                self.flush_barrier()
            except AuditError:
                pass
        with self._queue_cond:
            self._shutdown = True
            self._queue_cond.notify_all()
        if self._writer_thread is not None:
            self._writer_thread.join(BARRIER_TIMEOUT_S)
            if self._writer_thread.is_alive():
                # Never release the root while a delayed write can still run.
                self._fault(AuditError("audit_recording_failed", "writer did not stop"))
                return
        if self._writer_lock_fd is not None:
            fcntl.flock(self._writer_lock_fd, fcntl.LOCK_UN)
            os.close(self._writer_lock_fd)
            self._writer_lock_fd = None


class _CaptureEvidenceSink:
    def __init__(self, manager: AuditManager, titella_id: str) -> None:
        self.manager, self.titella_id = manager, titella_id

    def current_provenance(self) -> dict | None:
        with self.manager._lock:
            binding = self.manager._bindings.get(self.titella_id)
            if binding and binding.status == "active":
                return {"audit_id": binding.audit_id, "run_id": binding.run_id}
        return None

    def _identity(self) -> AuditCall | None:
        provenance = self.current_provenance()
        if not provenance:
            return None
        call = current_audit_call()
        if call and (call.audit_id, call.run_id, call.titella_id) == (provenance["audit_id"], provenance["run_id"], self.titella_id):
            return call
        return AuditCall(None, provenance["audit_id"], provenance["run_id"], self.titella_id, "screen",
                         time.monotonic(), utc_now_rfc3339())

    def record(self, image: bytes, metadata: Mapping, *, source: str, role: str = "capture", related: list[str] | None = None) -> dict | None:
        call = self._identity()
        if not call:
            return None
        try:
            digest = metadata.get("artifact_sha256") or (metadata.get("frame_sha256") if role == "capture" else None) or hashlib.sha256(image).hexdigest()
            artifact_metadata = {**dict(metadata), "artifact_sha256": digest}
            def write() -> str:
                eid = self.manager._artifact(call, role, image, "image/png", "png", metadata=artifact_metadata, related=related)
                if role == "capture":
                    self.manager._event(call.audit_id, call.run_id, call.titella_id, call.action_id, "capture.recorded",
                                        {"evidence_id": eid, "role": role, "source": source, "frame_sha256": metadata.get("frame_sha256", digest),
                                         "capture_resolution": metadata.get("capture_resolution"), "diff": metadata.get("diff"),
                                         "related_evidence_ids": related or []})
                return eid
            eid = self.manager._submit(write, len(image) + 512, audit_id=call.audit_id, wait=True)
            if not self.current_provenance() == {"audit_id": call.audit_id, "run_id": call.run_id}:
                return None
            if call.action_id is not None:
                with self.manager._lock:
                    self.manager._call_images.setdefault(call.action_id, []).append((eid, digest))
            return {"audit_id": call.audit_id, "run_id": call.run_id, "evidence_id": eid, "frame_sha256": digest,
                    "captured_at_utc": utc_now_rfc3339()}
        except BaseException as exc:
            self.manager._fault(exc, call.audit_id)
            return None
