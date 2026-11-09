"""Logical HID lease and keyboard/mouse operations."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config
from .screen import ScreenCapture
from .serial_link import SerialLink, TransportError

DEFAULT_LEASE_S = 600
HARD_MAX_LEASE_S = 3600


def _failure(code: str, message: str, *, retryable: bool = False, **details: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "error": {"code": code, "message": message, "retryable": retryable},
        **details,
    }


class HidLease:
    def __init__(self, config: Config) -> None:
        self.state_dir = config.ensure_state_dir()
        self.path = self.state_dir / "hid-lease.json"
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, value: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=".hid-lease-", dir=self.state_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, separators=(",", ":"))
            os.replace(name, self.path)
            self.path.chmod(0o600)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(name).unlink(missing_ok=True)
            raise

    def acquire(self, owner: str, lease_s: int = DEFAULT_LEASE_S) -> dict[str, Any]:
        lease_s = min(max(1, int(lease_s)), HARD_MAX_LEASE_S)
        now = time.time()
        with self._lock:
            state = self._read()
            holder = state.get("owner")
            expiry = float(state.get("expires", 0))
            if holder and expiry > now and holder != owner:
                return _failure(
                    "hid_lease_busy",
                    f"HID lease held by '{holder}'",
                    retryable=True,
                    owner=holder,
                    expires_in=int(expiry - now),
                )
            self._write({"owner": owner, "expires": now + lease_s, "lease": lease_s})
        return {"ok": True, "msg": f"HID lease acquired by '{owner}'", "lease_s": lease_s, "expires_in": lease_s}

    def renew(self, owner: str, lease_s: int = DEFAULT_LEASE_S) -> dict[str, Any]:
        lease_s = min(max(1, int(lease_s)), HARD_MAX_LEASE_S)
        with self._lock:
            state = self._read()
            if state.get("owner") != owner or float(state.get("expires", 0)) <= time.time():
                return _failure("hid_lease_not_owned", "the HID lease does not belong to the owner")
            self._write({"owner": owner, "expires": time.time() + lease_s, "lease": lease_s})
        return {"ok": True, "lease_s": lease_s}

    def release(self, owner: str) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            if state.get("owner") != owner:
                return _failure("hid_lease_not_owned", "the HID lease does not belong to the owner")
            self.path.unlink(missing_ok=True)
        return {"ok": True, "msg": "HID lease released"}

    def status(self, owner: str = "") -> dict[str, Any]:
        with self._lock:
            state = self._read()
            now = time.time()
            if state.get("owner") and float(state.get("expires", 0)) <= now:
                self.path.unlink(missing_ok=True)
                state = {}
        return {
            "ok": True,
            "locked": bool(state.get("owner")),
            "owner": state.get("owner"),
            "expires_in": max(0, int(float(state.get("expires", 0)) - now)),
            "you": state.get("owner") == owner if owner else None,
        }

    def require(self, owner: str) -> None:
        state = self._read()
        now = time.time()
        if not state.get("owner") or float(state.get("expires", 0)) <= now:
            raise TransportError("hid_lease_required", "acquire lock_acquire before using HID", False)
        if state.get("owner") != owner:
            raise TransportError("hid_lease_owned", f"the HID lease belongs to '{state.get('owner')}'", False)
        if float(state.get("expires", 0)) - now < float(state.get("lease", DEFAULT_LEASE_S)) / 2:
            self.renew(owner, int(state.get("lease", DEFAULT_LEASE_S)))


class HidController:
    def __init__(self, link: SerialLink, lease: HidLease, screen: ScreenCapture) -> None:
        self.link = link
        self.lease = lease
        self.screen = screen

    def _request(self, owner: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
        self.lease.require(owner)
        frame = self.link.request("hid", op, body)
        result = dict(frame.get("body") or {})
        result.setdefault("ok", True)
        if self.link.port:
            result["port"] = self.link.port
        return result

    def mouse_abs(self, owner: str, x: int, y: int) -> dict[str, Any]:
        resolution = self.screen.resolution()
        if resolution is None:
            return _failure("screen_required", "run screen first to calibrate coordinates")
        width, height = resolution
        if not 0 <= x < width or not 0 <= y < height:
            return _failure(
                "coordinates_out_of_bounds",
                f"coordinates outside the {width}x{height} capture",
            )
        hid_x = round(x * 32767 / (width - 1))
        hid_y = round(y * 32767 / (height - 1))
        result = self._request(owner, "mouse_abs", {"x": hid_x, "y": hid_y})
        result["coords"] = {"input_px": [x, y], "hid": [hid_x, hid_y], "capture_res": [width, height]}
        return result

    def mouse_rel(self, owner: str, x: int, y: int) -> dict[str, Any]:
        if not -127 <= x <= 127 or not -127 <= y <= 127:
            return _failure("invalid_argument", "mouse_rel requires x/y between -127 and 127")
        return self._request(owner, "mouse_rel", {"x": x, "y": y})

    def click(self, owner: str, button: str = "left", double: bool = False) -> dict[str, Any]:
        if button not in {"left", "right", "middle"}:
            return _failure("invalid_argument", "unknown button")
        return self._request(owner, "mouse_click", {"button": button, "double": bool(double)})

    def wheel(self, owner: str, delta: int) -> dict[str, Any]:
        if not -127 <= delta <= 127:
            return _failure("invalid_argument", "wheel requires a delta between -127 and 127")
        return self._request(owner, "mouse_wheel", {"delta": delta})

    def key(self, owner: str, key: str, mods: list[str] | None = None) -> dict[str, Any]:
        return self._request(owner, "kb_key", {"key": key, "mods": list(mods or [])})

    def type_text(self, owner: str, text: str, delay_ms: int = 10) -> dict[str, Any]:
        return self._request(owner, "kb_text", {"text": text, "delay": delay_ms, "enter": False})

    def keyboard_layout(self, owner: str, layout: str) -> dict[str, Any]:
        if layout not in {"es_ES", "en_US"}:
            return _failure("invalid_argument", "unsupported layout")
        return self._request(owner, "kb_layout", {"layout": layout})
