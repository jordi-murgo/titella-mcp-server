"""Sole owner of the master↔ESP32 link (UART or TCP), with correlation."""
from __future__ import annotations

import concurrent.futures
import errno
import fcntl
import glob
import itertools
import os
import queue
import secrets
import select
import socket
import struct
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config
from titella_slave_assets.wire import (
    DATA_CHUNK_BYTES,
    DATA_WINDOW_DEFAULT,
    DATA_WINDOW_MAX,
    FIRMWARE_TO_MASTER,
    MASTER_TO_FIRMWARE,
    MAX_OUTBOUND_FRAMES,
    MAX_REQUESTS,
    Deduplicator,
    FrameParser,
    WireError,
    base_frame,
    data_b64,
    decode_data,
    encode_frame,
)

SERIAL_PATTERNS = (
    "/dev/cu.wchusbserial*",
    "/dev/cu.usbserial*",
    "/dev/cu.usbmodem*",
    "/dev/ttyACM*",
    "/dev/ttyUSB*",
)
_BACKOFF = (0.5, 1.0, 2.0, 5.0)

# IOSSIOSPEED_32 (_IOW("T", 2, __uint32_t)) from the macOS SDK (IOKit
# serial/ioss.h): sets the real speed when termios exposes no constant
# (B460800 and above). The 64-bit variant is 0x80085402 and expects an
# 8-byte speed_t; this is the 32-bit one, with a 4-byte payload, the same
# one macOS serial clients use.
_IOSSIOSPEED = 0x80045402
_CUSTOM_BAUD_PLATFORMS = ("darwin",)


def _baud_setting(baud: int) -> tuple[int | None, str | None]:
    """Returns (termios constant, error) for ``baud``.

    A traditional speed uses the platform constant; a non-traditional one is
    only configurable where the system offers an ioctl for it, instead of
    silently accepting a different speed.
    """
    if not isinstance(baud, int) or baud <= 0:
        return None, f"serial_baud must be a positive integer: {baud!r}"
    constant = getattr(termios, f"B{baud}", None)
    if constant is not None:
        return constant, None
    if sys.platform in _CUSTOM_BAUD_PLATFORMS:
        return None, None
    return None, (
        f"serial_baud={baud} has no termios constant on {sys.platform}; "
        "use a traditional speed or macOS, the only system with IOSSIOSPEED"
    )


class TransportError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False, **context: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.context = context

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, **self.context}


class SerialLink:
    """Permanent reader/writer threads; no other class opens the transport."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.transport = "wifi" if config.transport == "wifi" else "serial"
        if self.transport == "serial":
            constant, error = _baud_setting(config.serial_baud)
            if error is not None:
                raise ValueError(error)
            self._baud_constant = constant
        else:
            # TCP has no baud rate: a speed without a termios constant must
            # not prevent using the Wi-Fi transport on this platform.
            self._baud_constant = None
        self.state_dir = config.ensure_state_dir()
        self.link_path = self.state_dir / "active-link-id"
        self.link_id = self._load_or_create_link_id()
        self.port: str | None = None
        self._fd: int | None = None
        self._fd_lock = threading.Lock()
        self._stop = threading.Event()
        self._started = threading.Event()
        self._connected = threading.Event()
        self._outbound: queue.PriorityQueue[tuple[int, int, dict[str, Any]]] = queue.PriorityQueue(MAX_OUTBOUND_FRAMES)
        self._order = itertools.count()
        self._request_ids = itertools.count(1)
        self._pending: dict[int, tuple[str, concurrent.futures.Future[dict[str, Any]]]] = {}
        self._pending_lock = threading.Lock()
        self._acks: dict[tuple[str, int], concurrent.futures.Future[None]] = {}
        self._acks_lock = threading.Lock()
        self._ack_cond = threading.Condition(self._acks_lock)
        self._data_lock = threading.Lock()
        self._handlers: dict[tuple[str, str | None], list[Callable[[dict[str, Any]], Any]]] = {}
        self._handlers_lock = threading.Lock()
        self._state_handlers: list[Callable[[str], Any]] = []
        self._state_lock = threading.Lock()
        self.state = "idle"
        self._dedup = Deduplicator()
        self._parser = FrameParser(FIRMWARE_TO_MASTER, ignore_invalid=True)
        self._threads: list[threading.Thread] = []
        self.last_seen = 0.0
        self.disconnected_since: float | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            if self.state == state:
                return
            self.state = state
            handlers = list(self._state_handlers)
        for handler in handlers:
            try:
                handler(state)
            except Exception:
                pass

    def subscribe_state(self, handler: Callable[[str], Any]) -> Callable[[], None]:
        with self._state_lock:
            self._state_handlers.append(handler)
            state = self.state
        handler(state)

        def unsubscribe() -> None:
            with self._state_lock:
                if handler in self._state_handlers:
                    self._state_handlers.remove(handler)

        return unsubscribe

    def _load_or_create_link_id(self) -> str:
        try:
            value = self.link_path.read_text(encoding="ascii").strip()
            if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
                return value
        except OSError:
            pass
        value = secrets.token_hex(16)
        fd = os.open(self.link_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, value.encode("ascii"))
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return value

    def start(self) -> None:
        if self._started.is_set():
            return
        self._started.set()
        self._set_state("connecting")
        self._threads = [
            threading.Thread(target=self._reader_loop, name="hid-uart-reader", daemon=True),
            threading.Thread(target=self._writer_loop, name="hid-uart-writer", daemon=True),
            threading.Thread(target=self._heartbeat_loop, name="hid-uart-heartbeat", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def close(self, clean: bool = True) -> None:
        self._stop.set()
        self._connected.set()
        self._close_fd()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        self._fail_pending(TransportError("shutdown", "link closed", False), channels=None)
        self._fail_acks(TransportError("shutdown", "link closed", False))
        self._set_state("closed")
        if clean:
            self.link_path.unlink(missing_ok=True)

    def wait_connected(self, timeout: float = 6.0) -> bool:
        self.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connected and self._fd is not None:
                return True
            if self._stop.wait(0.02):
                break
        return False

    def _discover_port(self) -> str:
        if self.config.serial_port:
            if not Path(self.config.serial_port).exists():
                raise FileNotFoundError(f"TITELLA_SERIAL_PORT does not exist: {self.config.serial_port}")
            return self.config.serial_port
        for pattern in SERIAL_PATTERNS:
            candidates = sorted(glob.glob(pattern))
            if candidates:
                return candidates[0]
        raise FileNotFoundError("Titella not found; define TITELLA_SERIAL_PORT")

    def _open(self) -> None:
        if self.transport == "wifi":
            fd, label = self._open_wifi()
        else:
            fd, label = self._open_serial()
        self._adopt(fd, label)

    def _open_serial(self) -> tuple[int, str]:
        port = self._discover_port()
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            if hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(fd, termios.TIOCEXCL)
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0
            # A non-traditional speed has no termios constant: leave any valid
            # one and let IOSSIOSPEED set the real speed afterwards.
            speed = self._baud_constant
            if speed is None:
                speed = termios.B115200
            attrs[4] = speed
            attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            if self._baud_constant is None:
                # IOSSIOSPEED replaces termios ispeed/ospeed. An error here must
                # abort the open: continuing would leave the UART at 115200
                # while the master believes it talks at the configured speed.
                fcntl.ioctl(fd, _IOSSIOSPEED, struct.pack("I", self.config.serial_baud))
            # On open, the driver asserts DTR and RTS. On the WCH bridge that
            # leaves EN low and the ESP32 stays in reset: zero bytes and no
            # frame. Releasing both lines lets it boot normally. Ptys and other
            # platforms may not support modem control; in that case the open is
            # still valid.
            if hasattr(termios, "TIOCMBIC"):
                try:
                    lines = termios.TIOCM_DTR | termios.TIOCM_RTS
                    fcntl.ioctl(fd, termios.TIOCMBIC, struct.pack("I", lines))
                except OSError:
                    pass
        except BaseException:
            os.close(fd)
            raise
        return fd, port

    def _open_wifi(self) -> tuple[int, str]:
        """Connects to the firmware SoftAP. No autodetection and no modem lines."""
        host = self.config.wifi_host
        port = self.config.wifi_port
        connection = socket.create_connection((host, port), timeout=3.0)
        try:
            # Frames are small JSON lines: Nagle would only add latency to
            # interactive HID responses.
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.setblocking(False)
        except BaseException:
            connection.close()
            raise
        return connection.detach(), f"{host}:{port}"

    def _adopt(self, fd: int, label: str) -> None:
        with self._fd_lock:
            if self._stop.is_set():
                os.close(fd)
                return
            self._fd = fd
            self.port = label
            self._parser = FrameParser(FIRMWARE_TO_MASTER, ignore_invalid=True)
            if self.disconnected_since is None and self.last_seen == 0:
                self.last_seen = time.monotonic()
                self._set_state("connecting")
            else:
                self._set_state("reconnecting")
            self._connected.set()

    def _close_fd(self) -> None:
        with self._fd_lock:
            fd, self._fd = self._fd, None
            self.port = None
            self._connected.clear()
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _transport_name(self) -> str:
        return "TCP" if self.transport == "wifi" else "UART"

    def _transport_lost(self, exc: BaseException) -> None:
        if self.disconnected_since is None:
            self.disconnected_since = time.monotonic()
        self._close_fd()
        self._set_state("reconnecting")
        self._fail_pending(
            TransportError(
                "transport_disconnected",
                f"{self._transport_name()} disconnected: {exc}",
                True,
            ),
            channels={"hid"},
        )

    def _reader_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if self._fd is None:
                try:
                    self._open()
                    attempt = 0
                except (OSError, ValueError) as exc:
                    delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                    attempt += 1
                    self._stop.wait(delay)
                    continue
            with self._fd_lock:
                fd = self._fd
            if fd is None:
                continue
            try:
                readable, _, _ = select.select([fd], [], [], 0.25)
                if not readable:
                    continue
                chunk = os.read(fd, 4096)
                if not chunk:
                    raise OSError(errno.EIO, f"EOF from {self._transport_name()}")
                for frame in self._parser.feed(chunk):
                    self._dispatch(frame)
            except BlockingIOError:
                # select woke up without data (possible on sockets): retry.
                continue
            except (OSError, ValueError) as exc:
                self._transport_lost(exc)

    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                priority, order, frame = self._outbound.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                while not self._stop.is_set():
                    if not self._connected.wait(0.25):
                        continue
                    with self._fd_lock:
                        fd = self._fd
                    if fd is None:
                        continue
                    payload = encode_frame(frame, MASTER_TO_FIRMWARE)
                    sent = 0
                    try:
                        while sent < len(payload):
                            _, writable, _ = select.select([], [fd], [], 1.0)
                            if not writable:
                                raise TimeoutError("timeout writing to the link")
                            try:
                                written = os.write(fd, payload[sent:])
                            except BlockingIOError:
                                continue
                            if written <= 0:
                                raise OSError(errno.EIO, "short write on the link")
                            sent += written
                        break
                    except (OSError, TimeoutError) as exc:
                        self._transport_lost(exc)
            except WireError as exc:
                request_id = frame.get("request_id")
                if isinstance(request_id, int):
                    self._fail_request(request_id, TransportError(exc.code, exc.message, False))
            finally:
                self._outbound.task_done()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.config.heartbeat_s):
            if self.connected:
                try:
                    self.send_frame(base_frame(self.link_id, "control", "heartbeat"))
                except TransportError:
                    pass
            now = time.monotonic()
            grace_origin = self.disconnected_since or self.last_seen
            if grace_origin and now - grace_origin > self.config.disconnect_grace_s:
                self._set_state("disconnected")
                expired = TransportError("disconnect_grace_expired", "the agent is not responding", True)
                self._fail_pending(expired, channels=None)
                self._fail_acks(expired)

    def _priority(self, frame: dict[str, Any]) -> int:
        if frame["type"] in {"ack", "heartbeat"} or frame["channel"] == "hid":
            return 0
        if frame["type"] in {"request", "response", "event", "error"}:
            return 1
        if frame["channel"] == "shell":
            return 2
        return 3

    def send_frame(self, frame: dict[str, Any]) -> None:
        if self._stop.is_set():
            raise TransportError("shutdown", "link closed", False)
        self.start()
        try:
            encode_frame(frame, MASTER_TO_FIRMWARE)
            self._outbound.put_nowait((self._priority(frame), next(self._order), frame))
        except queue.Full as exc:
            raise TransportError("queue_full", "send queue is full", True) from exc
        except WireError as exc:
            raise TransportError(exc.code, exc.message, False) from exc

    def request(self, channel: str, op: str, body: dict[str, Any] | None = None, timeout: float = 6.0) -> dict[str, Any]:
        with self._pending_lock:
            if len(self._pending) >= MAX_REQUESTS:
                raise TransportError("too_many_requests", "too many pending requests", True)
            request_id = next(self._request_ids)
            future: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
            self._pending[request_id] = (channel, future)
        frame = base_frame(
            self.link_id,
            channel,
            "request",
            request_id=request_id,
            op=op,
            body=body or {},
        )
        try:
            self.send_frame(frame)
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            self._fail_request(request_id, TransportError("request_timeout", f"timeout on {channel}.{op}", True, request_id=request_id))
            raise TransportError("request_timeout", f"timeout on {channel}.{op}", True, request_id=request_id) from exc
        except BaseException:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise

    def send_data(self, channel: str, session_id: str, seq: int, data: bytes) -> None:
        if len(data) > DATA_CHUNK_BYTES:
            raise TransportError("chunk_too_large", "chunk over 512 bytes", False)
        key = (session_id, seq)
        with self._data_lock:
            for attempt in range(self.config.ack_retries + 1):
                future: concurrent.futures.Future[None] = concurrent.futures.Future()
                with self._acks_lock:
                    self._acks[key] = future
                self.send_frame(base_frame(
                    self.link_id,
                    channel,
                    "data",
                    session_id=session_id,
                    seq=seq,
                    data_b64=data_b64(data),
                ))
                try:
                    future.result(timeout=self.config.ack_timeout_s)
                    return
                except concurrent.futures.TimeoutError:
                    continue
                finally:
                    with self._acks_lock:
                        if self._acks.get(key) is future:
                            self._acks.pop(key, None)
            raise TransportError("ack_timeout", f"no ACK for {session_id}:{seq}", True, session_id=session_id)

    def stream_data(
        self,
        channel: str,
        session_id: str,
        read_chunk: Callable[[], bytes],
        window: int = DATA_WINDOW_DEFAULT,
        on_ack: Callable[[int], None] | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> int:
        """Sends a stream with a sliding window of individual ACKs.

        ``read_chunk`` returns the next block (<=512 bytes) or ``b""`` at the
        end; it is read only when there is room in the window, so read-ahead is
        bounded to ``window`` blocks. Each block keeps its individual ACK: an
        ACK frees its slot as soon as it arrives, even when ACKs arrive out of
        order. Expired blocks are resent in sequence order and the attempt is
        exhausted after ``ack_retries`` resends.

        Returns the acknowledged bytes. Does not take ``_data_lock``: a file
        must not monopolize interactive sends.
        """
        if window < 1 or window > DATA_WINDOW_MAX:
            raise ValueError(f"window must be between 1 and {DATA_WINDOW_MAX}")
        # seq -> [data, future, retries, deadline]
        pending: dict[int, list[Any]] = {}
        next_seq = 0
        eof = False
        acked_bytes = 0
        timeout_s = self.config.ack_timeout_s
        try:
            while pending or not eof:
                if stop_check is not None and stop_check():
                    raise TransportError(
                        "cancelled", "transfer cancelled", False, session_id=session_id,
                    )
                while not eof and len(pending) < window:
                    chunk = read_chunk()
                    if not chunk:
                        eof = True
                        break
                    if len(chunk) > DATA_CHUNK_BYTES:
                        raise TransportError("chunk_too_large", "chunk over 512 bytes", False)
                    seq = next_seq
                    next_seq += 1
                    future: concurrent.futures.Future[None] = concurrent.futures.Future()
                    with self._acks_lock:
                        self._acks[(session_id, seq)] = future
                    pending[seq] = [chunk, future, 0, time.monotonic() + timeout_s]
                    self.send_frame(base_frame(
                        self.link_id,
                        channel,
                        "data",
                        session_id=session_id,
                        seq=seq,
                        data_b64=data_b64(chunk),
                    ))
                if not pending:
                    break
                # Wake up as soon as any ACK arrives, without losing notices:
                # checked under the same lock as resolution.
                with self._ack_cond:
                    while True:
                        if any(entry[1].done() for entry in pending.values()):
                            break
                        remaining = min(entry[3] for entry in pending.values()) - time.monotonic()
                        if remaining <= 0:
                            break
                        self._ack_cond.wait(remaining)
                now = time.monotonic()
                for seq in [item for item in pending if pending[item][1].done()]:
                    data, future, _attempts, _deadline = pending.pop(seq)
                    with self._acks_lock:
                        self._acks.pop((session_id, seq), None)
                    exc = future.exception()
                    if exc is not None:
                        raise exc
                    acked_bytes += len(data)
                    if on_ack is not None:
                        on_ack(acked_bytes)
                for seq in sorted(item for item in pending if now >= pending[item][3]):
                    data, future, attempts, _deadline = pending[seq]
                    if attempts >= self.config.ack_retries:
                        raise TransportError(
                            "ack_timeout",
                            f"no ACK for {session_id}:{seq}",
                            True,
                            session_id=session_id,
                        )
                    pending[seq][2] = attempts + 1
                    pending[seq][3] = now + timeout_s
                    self.send_frame(base_frame(
                        self.link_id,
                        channel,
                        "data",
                        session_id=session_id,
                        seq=seq,
                        data_b64=data_b64(data),
                    ))
            return acked_bytes
        finally:
            with self._acks_lock:
                for seq in list(pending):
                    future = pending[seq][1]
                    self._acks.pop((session_id, seq), None)
                    if not future.done():
                        future.set_exception(TransportError(
                            "cancelled", "transfer interrupted", False,
                            session_id=session_id,
                        ))

    def subscribe(self, channel: str, handler: Callable[[dict[str, Any]], Any], session_id: str | None = None) -> Callable[[], None]:
        key = (channel, session_id)
        with self._handlers_lock:
            self._handlers.setdefault(key, []).append(handler)

        def unsubscribe() -> None:
            with self._handlers_lock:
                handlers = self._handlers.get(key, [])
                if handler in handlers:
                    handlers.remove(handler)
                if not handlers:
                    self._handlers.pop(key, None)

        return unsubscribe

    def _dispatch(self, frame: dict[str, Any]) -> None:
        if frame["link_id"] != self.link_id:
            return
        self.last_seen = time.monotonic()
        self.disconnected_since = None
        self._set_state("connected")
        kind = frame["type"]
        if kind in {"response", "error"} and isinstance(frame.get("request_id"), int):
            if kind == "error":
                body = frame.get("body") or {}
                if body.get("code") == "route_error" and frame["channel"] != "hid":
                    self._set_state("agent_unreachable")
            request_id = frame["request_id"]
            with self._pending_lock:
                pending = self._pending.pop(request_id, None)
            if pending is not None:
                _, future = pending
                if kind == "error":
                    body = frame.get("body") or {}
                    remote_error = TransportError(
                        str(body.get("code", "remote_error")),
                        str(body.get("message", "remote error")),
                        bool(body.get("retryable", False)),
                        request_id=request_id,
                    )
                    future.set_exception(remote_error)
                else:
                    future.set_result(frame)
            return
        if kind == "ack":
            key = (frame["session_id"], frame["ack_seq"])
            with self._acks_lock:
                future = self._acks.get(key)
                if future is not None and not future.done():
                    future.set_result(None)
                self._ack_cond.notify_all()
            return
        if kind == "data":
            key = (frame["link_id"], frame["session_id"], frame["seq"])
            # An already-applied duplicate is acknowledged again without
            # reapplying its bytes. A new frame is registered ONLY if a handler
            # accepted it: registering before acceptance would turn the resend
            # of a rejected slot into a false ACK, silently losing data.
            if not self._dedup.seen(key):
                if not self._notify(frame):
                    return
                self._dedup.add(key)
            self.send_frame(base_frame(
                self.link_id,
                frame["channel"],
                "ack",
                session_id=frame["session_id"],
                ack_seq=frame["seq"],
            ))
            return
        self._notify(frame)

    def _notify(self, frame: dict[str, Any]) -> bool:
        session_id = frame.get("session_id") if isinstance(frame.get("session_id"), str) else None
        with self._handlers_lock:
            handlers = list(self._handlers.get((frame["channel"], session_id), ()))
            handlers += list(self._handlers.get((frame["channel"], None), ()))
        try:
            for handler in handlers:
                if handler(frame) is False:
                    return False
        except Exception:
            return False
        return True

    def _fail_request(self, request_id: int, exc: TransportError) -> None:
        with self._pending_lock:
            pending = self._pending.pop(request_id, None)
        if pending is not None and not pending[1].done():
            pending[1].set_exception(exc)

    def _fail_acks(self, exc: TransportError) -> None:
        # Under the same lock as ACK resolution: a future cannot be resolved
        # and failed at the same time (InvalidStateError in the reader).
        with self._acks_lock:
            futures = list(self._acks.values())
            self._acks.clear()
            for future in futures:
                if not future.done():
                    future.set_exception(exc)
            self._ack_cond.notify_all()

    def _fail_pending(self, exc: TransportError, channels: set[str] | None) -> None:
        with self._pending_lock:
            selected = [
                (request_id, future)
                for request_id, (channel, future) in self._pending.items()
                if channels is None or channel in channels
            ]
            for request_id, _ in selected:
                self._pending.pop(request_id, None)
        for _, future in selected:
            if not future.done():
                future.set_exception(exc)
