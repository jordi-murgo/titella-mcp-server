"""Firmware identity query that does not depend on the HID lease."""
from __future__ import annotations

import threading
import time
from typing import Any

from .serial_link import SerialLink, TransportError

CACHE_TTL_S = 5.0


class FirmwareInfo:
    """Caches the ESP32 health so it is not queried on every HTTP request."""

    def __init__(self, link: SerialLink) -> None:
        self._link = link
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def snapshot(self, timeout: float = 2.0) -> dict[str, Any]:
        # The link starts lazily on the first request; the probe must trigger
        # that connection or /health would never reflect the firmware.
        self._link.start()
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now - self._cached_at < CACHE_TTL_S:
                return dict(self._cached)
        try:
            frame = self._link.request("hid", "health", {}, timeout=timeout)
        except TransportError as exc:
            result: dict[str, Any] = {
                "reachable": False,
                "error": {"code": exc.code, "message": exc.message},
            }
        else:
            body = dict(frame.get("body") or {})
            result = {
                "reachable": True,
                "firmware": body.get("firmware"),
                "protocol": body.get("protocol"),
                "uart_baud": body.get("uart_baud"),
                "port": self._link.port,
                "transport": getattr(self._link, "transport", "serial"),
                "wifi": body.get("wifi"),
            }
        with self._lock:
            self._cached = result
            self._cached_at = time.monotonic()
        return dict(result)
