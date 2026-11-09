"""Master client for system, files and remote transfers."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_state import AgentGate
from .serial_link import SerialLink, TransportError
from titella_slave_assets.wire import DATA_CHUNK_BYTES, DATA_WINDOW_DEFAULT, DATA_WINDOW_MAX, decode_data

MAX_CONVENIENCE_WRITE = 64 * 1024
MAX_TRANSFER_BYTES = 512 * 1024 * 1024


def _validated_window(window: int) -> int:
    """Internal transfer window; 1 reproduces stop-and-wait."""
    if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= DATA_WINDOW_MAX:
        raise ValueError(f"window must be an integer between 1 and {DATA_WINDOW_MAX}")
    return window


@dataclass
class StreamCollector:
    payload: bytearray = field(default_factory=bytearray)
    result: dict[str, Any] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class TransferState:
    transfer_id: str
    direction: str
    status: str = "running"
    bytes_total: int = 0
    bytes_transferred: int = 0
    sha256: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    remote_done: threading.Event = field(default_factory=threading.Event)
    stream: Any = None
    temporary: Path | None = None
    final_path: Path | None = None
    hasher: Any = field(default_factory=hashlib.sha256)
    next_seq: int = 0


class FilesClient:
    def __init__(self, link: SerialLink, gate: AgentGate | None = None) -> None:
        self.link = link
        self.gate = gate or AgentGate()
        self._streams: dict[str, StreamCollector] = {}
        self._transfers: dict[str, TransferState] = {}
        self._lock = threading.Lock()
        self._unsubscribe_fs = link.subscribe("fs", self._on_fs)
        self._unsubscribe_transfer = link.subscribe("transfer", self._on_transfer)

    def close(self) -> None:
        self._unsubscribe_fs()
        self._unsubscribe_transfer()
        with self._lock:
            transfers = list(self._transfers.values())
        for transfer in transfers:
            transfer.cancelled.set()
            if transfer.stream:
                try:
                    transfer.stream.close()
                except OSError:
                    pass
            if transfer.temporary:
                transfer.temporary.unlink(missing_ok=True)

    def _on_fs(self, frame: dict[str, Any]) -> bool:
        session_id = frame.get("session_id")
        if not isinstance(session_id, str):
            return True
        with self._lock:
            collector = self._streams.get(session_id)
        if collector is None:
            return False
        if frame["type"] == "data":
            collector.payload.extend(decode_data(frame))
        elif frame["type"] == "event" and frame.get("op") == "fs.stream.finished":
            collector.result = dict(frame.get("body") or {})
            collector.done.set()
        return True

    def _on_transfer(self, frame: dict[str, Any]) -> bool:
        transfer_id = frame.get("session_id")
        if not isinstance(transfer_id, str):
            return True
        with self._lock:
            transfer = self._transfers.get(transfer_id)
        if transfer is None:
            return False
        if frame["type"] == "data":
            # Cancelled or without a destination: nothing is written or acknowledged.
            if transfer.cancelled.is_set() or transfer.stream is None:
                return False
            if frame["seq"] < transfer.next_seq:
                # Block already written: its ACK was lost and the resend arrived
                # late, possibly outside the global dedup window. It is
                # acknowledged again without rewriting bytes (idempotent, like
                # upload_data(seq < expected)).
                return True
            if frame["seq"] != transfer.next_seq:
                # Gap: nothing is written or acknowledged; the sender will resend.
                return False
            data = decode_data(frame)
            transfer.stream.write(data)
            transfer.hasher.update(data)
            transfer.bytes_transferred += len(data)
            transfer.next_seq += 1
        elif frame["type"] == "event" and frame.get("op") in {"transfer.finished", "transfer.progress"}:
            transfer.result = dict(frame.get("body") or {})
            if frame.get("op") == "transfer.finished":
                transfer.remote_done.set()
        return True

    def _request(self, op: str, **body: Any) -> dict[str, Any]:
        self.gate.require()
        stream_id = uuid.uuid4().hex
        collector = StreamCollector()
        with self._lock:
            self._streams[stream_id] = collector
        try:
            response = self.link.request("fs", op, {**body, "stream_id": stream_id}, timeout=65)
            result = dict(response.get("body") or {})
            payload_kind = result.get("payload_kind")
            if payload_kind not in {"bytes", "json"}:
                return result
            if not collector.done.wait(65):
                raise TransportError("stream_timeout", f"timeout receiving fs.{op}", True, session_id=stream_id)
            if collector.result.get("status") != "finished":
                raise TransportError(
                    str(collector.result.get("code", "stream_failed")),
                    str(collector.result.get("message", "fs stream failed")),
                    bool(collector.result.get("retryable", True)),
                    session_id=stream_id,
                )
            if payload_kind == "json":
                return json.loads(bytes(collector.payload).decode("utf-8"))
            result["_bytes"] = bytes(collector.payload)
            return result
        finally:
            with self._lock:
                self._streams.pop(stream_id, None)

    def read(self, path: str, offset: int = 0, length: int = 32768, encoding: str = "utf-8") -> dict[str, Any]:
        result = self._request("read", path=path, offset=offset, length=length)
        data = result.pop("_bytes")
        if encoding == "base64":
            result["data"] = base64.b64encode(data).decode("ascii")
            result["encoding"] = "base64"
        else:
            result["data"] = data.decode(encoding, errors="replace")
            result["encoding"] = encoding
        return result

    def write(self, path: str, data: str, encoding: str = "utf-8") -> dict[str, Any]:
        raw = base64.b64decode(data, validate=True) if encoding == "base64" else data.encode(encoding)
        if len(raw) > MAX_CONVENIENCE_WRITE:
            raise ValueError("remote_fs_write accepts up to 64 KiB")
        transfer_id = uuid.uuid4().hex
        digest = hashlib.sha256(raw).hexdigest()
        self.link.request("transfer", "upload.begin", {
            "transfer_id": transfer_id, "path": path, "size": len(raw), "sha256": digest,
        }, timeout=10)
        state = {"position": 0}

        def read_chunk() -> bytes:
            position = state["position"]
            if position >= len(raw):
                return b""
            state["position"] = position + DATA_CHUNK_BYTES
            return raw[position:position + DATA_CHUNK_BYTES]

        self.link.stream_data("transfer", transfer_id, read_chunk)
        response = self.link.request(
            "transfer", "upload.commit", {"transfer_id": transfer_id}, timeout=10,
        )
        result = dict(response.get("body") or {})
        result.update({"status": "written", "path": path, "bytes": len(raw), "sha256": digest})
        return result

    def list(self, path: str, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        return self._request("list", path=path, offset=offset, limit=limit)

    def delete(self, path: str) -> dict[str, Any]:
        return self._request("delete", path=path)

    def replace(self, path: str, old: str, new: str) -> dict[str, Any]:
        return self._request("replace", path=path, old=old, new=new)

    def search(self, path: str, query: str, limit: int = 200) -> dict[str, Any]:
        return self._request("search", path=path, query=query, limit=limit)

    def find(self, path: str, pattern: str, limit: int = 500) -> dict[str, Any]:
        return self._request("find", path=path, pattern=pattern, limit=limit)

    def sysinfo(self) -> dict[str, Any]:
        return self._request("sysinfo")

    def upload(
        self,
        local_path: str,
        remote_path: str,
        sync_timeout: float = 10,
        window: int = DATA_WINDOW_DEFAULT,
    ) -> dict[str, Any]:
        self.gate.require()
        window = _validated_window(window)
        source = Path(local_path).expanduser()
        size = source.stat().st_size
        if size > MAX_TRANSFER_BYTES:
            raise ValueError("the transfer exceeds 512 MiB")
        transfer = TransferState(uuid.uuid4().hex, "upload", bytes_total=size)
        with self._lock:
            self._transfers[transfer.transfer_id] = transfer
        threading.Thread(
            target=self._upload_worker,
            args=(transfer, source, remote_path, window),
            name=f"upload-{transfer.transfer_id[:8]}",
            daemon=True,
        ).start()
        return self._wait_transfer(transfer, sync_timeout)

    def _upload_worker(
        self,
        transfer: TransferState,
        source: Path,
        remote_path: str,
        window: int = DATA_WINDOW_DEFAULT,
    ) -> None:
        try:
            hasher = hashlib.sha256()
            with source.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
            transfer.sha256 = hasher.hexdigest()
            self.link.request("transfer", "upload.begin", {
                "transfer_id": transfer.transfer_id,
                "path": remote_path,
                "size": transfer.bytes_total,
                "sha256": transfer.sha256,
            }, timeout=10)
            with source.open("rb") as stream:
                def read_chunk() -> bytes:
                    if transfer.cancelled.is_set():
                        raise TransportError(
                            "cancelled", "upload cancelled", False, session_id=transfer.transfer_id,
                        )
                    return stream.read(DATA_CHUNK_BYTES)

                def on_ack(total: int) -> None:
                    transfer.bytes_transferred = total

                self.link.stream_data(
                    "transfer",
                    transfer.transfer_id,
                    read_chunk,
                    window=window,
                    on_ack=on_ack,
                    stop_check=transfer.cancelled.is_set,
                )
            if transfer.cancelled.is_set():
                transfer.status = "cancelled"
                return
            response = self.link.request(
                "transfer", "upload.commit", {"transfer_id": transfer.transfer_id}, timeout=30,
            )
            transfer.result = dict(response.get("body") or {})
            transfer.status = str(transfer.result.get("status", "finished"))
        except BaseException as exc:
            self._transfer_failed(transfer, exc)
        finally:
            transfer.done.set()

    def download(
        self,
        remote_path: str,
        local_path: str,
        checksum: str | None = None,
        sync_timeout: float = 10,
        window: int = DATA_WINDOW_DEFAULT,
    ) -> dict[str, Any]:
        self.gate.require()
        window = _validated_window(window)
        destination = Path(local_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        transfer = TransferState(uuid.uuid4().hex, "download", final_path=destination)
        with self._lock:
            self._transfers[transfer.transfer_id] = transfer
        threading.Thread(
            target=self._download_worker,
            args=(transfer, remote_path, checksum, window),
            name=f"download-{transfer.transfer_id[:8]}",
            daemon=True,
        ).start()
        return self._wait_transfer(transfer, sync_timeout)

    def _download_worker(
        self,
        transfer: TransferState,
        remote_path: str,
        checksum: str | None,
        window: int = DATA_WINDOW_DEFAULT,
    ) -> None:
        try:
            fd, name = tempfile.mkstemp(
                prefix=f".{transfer.final_path.name}.titella-",
                dir=transfer.final_path.parent,
            )
            transfer.temporary = Path(name)
            transfer.stream = os.fdopen(fd, "wb")
            response = self.link.request("transfer", "download.begin", {
                "transfer_id": transfer.transfer_id,
                "path": remote_path,
                "checksum": checksum,
                "window": window,
            }, timeout=30)
            metadata = dict(response.get("body") or {})
            transfer.bytes_total = int(metadata["bytes_total"])
            transfer.sha256 = str(metadata["sha256"])
            # Sliced wait: cancellation or link close must end the receiver
            # even when the agent does not send the final event.
            deadline = time.monotonic() + 3600
            while not transfer.remote_done.wait(0.25):
                if transfer.cancelled.is_set():
                    raise TransportError(
                        "cancelled", "download cancelled", False, session_id=transfer.transfer_id,
                    )
                if time.monotonic() >= deadline:
                    raise TransportError("transfer_timeout", "download timeout", True)
            transfer.stream.flush()
            os.fsync(transfer.stream.fileno())
            transfer.stream.close()
            transfer.stream = None
            if transfer.cancelled.is_set():
                transfer.status = "cancelled"
                return
            if transfer.result.get("status") != "finished":
                raise TransportError("download_failed", "the agent did not complete the download", True)
            if transfer.bytes_transferred != transfer.bytes_total:
                raise TransportError("size_mismatch", "downloaded size is wrong", True)
            if transfer.hasher.hexdigest() != transfer.sha256:
                raise TransportError("checksum_mismatch", "downloaded SHA-256 is wrong", True)
            os.replace(transfer.temporary, transfer.final_path)
            transfer.temporary = None
            transfer.status = "finished"
        except BaseException as exc:
            self._transfer_failed(transfer, exc)
        finally:
            if transfer.stream:
                transfer.stream.close()
                transfer.stream = None
            if transfer.temporary:
                transfer.temporary.unlink(missing_ok=True)
            transfer.done.set()

    def _transfer_failed(self, transfer: TransferState, exc: BaseException) -> None:
        if transfer.cancelled.is_set():
            transfer.status = "cancelled"
            return
        transfer.status = "error"
        if isinstance(exc, TransportError):
            transfer.error = exc.as_dict()
        else:
            transfer.error = {"code": "transfer_failed", "message": str(exc), "retryable": True}

    def _wait_transfer(self, transfer: TransferState, sync_timeout: float) -> dict[str, Any]:
        transfer.done.wait(max(0, sync_timeout))
        return self.transfer_status(transfer.transfer_id)

    def transfer_status(self, transfer_id: str) -> dict[str, Any]:
        with self._lock:
            transfer = self._transfers.get(transfer_id)
        if transfer is None:
            return {"status": "not_found", "transfer_id": transfer_id}
        result = {
            "status": transfer.status,
            "transfer_id": transfer.transfer_id,
            "direction": transfer.direction,
            "bytes_total": transfer.bytes_total,
            "bytes_transferred": transfer.bytes_transferred,
        }
        if transfer.sha256:
            result["sha256"] = transfer.sha256
        if transfer.error:
            result["error"] = transfer.error
        result.update(transfer.result)
        return result

    def transfer_cancel(self, transfer_id: str) -> dict[str, Any]:
        with self._lock:
            transfer = self._transfers.get(transfer_id)
        if transfer is None:
            return {"status": "not_found", "transfer_id": transfer_id}
        if transfer.status != "running":
            return self.transfer_status(transfer_id)
        transfer.cancelled.set()
        try:
            self.link.request("transfer", "cancel", {"transfer_id": transfer_id}, timeout=10)
        except TransportError:
            pass
        transfer.status = "cancelled"
        return self.transfer_status(transfer_id)
