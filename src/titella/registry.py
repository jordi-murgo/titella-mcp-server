"""Runtime registry of Titella instances and hardware discovery."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent_state import AgentGate
from .bootstrap import BootstrapManager
from .config import (
    DEFAULT_TITELLA_ID,
    Config,
    _INSTANCE_KEYS,
    _read_raw_toml,
    _split_document,
    instance_state_dir,
    load_config,
    save_titella_config,
    validate_titella_id,
)
from .diagnostics import diagnose
from .files_client import FilesClient
from .firmware_info import FirmwareInfo
from .hid import HidController, HidLease
from .screen import ScreenCapture
from .serial_link import SerialLink, TransportError
from .shell_client import ShellClient

# Known USB vendor IDs for Titella master/native bridges:
# 0x1A86 = QinHeng / WCH (UART programming bridge)
# 0x303A = Espressif Systems (native USB-OTG)
KNOWN_USB_VIDS = frozenset({0x1A86, 0x303A})


class TitellaInstance:
    """Self-contained runtime state of one Titella instance."""

    def __init__(
        self,
        config: Config,
        *,
        link: SerialLink | None = None,
        firmware: FirmwareInfo | None = None,
    ) -> None:
        self.config = config
        self.titella_id = config.titella_id
        self._seed_state_dir_if_migrating(config)
        self.serial = link or SerialLink(config)
        self.firmware = firmware or FirmwareInfo(self.serial)
        self.capture = ScreenCapture(config)
        self.lease = HidLease(config)
        self.hid = HidController(self.serial, self.lease, self.capture)
        self.gate = AgentGate()
        self.bootstrap = BootstrapManager(config, self.serial, self.hid, self.gate)
        self._bind_lock = threading.Lock()
        self._binding = False
        self._unsubscribe_agent_state = self.serial.subscribe_state(
            self._on_agent_link_state
        )

        self.serial.subscribe("control", self._attach)
        if link is None:
            threading.Thread(
                target=self._reattach,
                name=f"agent-reattach-{self.titella_id}",
                daemon=True,
            ).start()
        else:
            self._reattach()

        self.shells = ShellClient(self.serial, self.gate)
        self.files = FilesClient(self.serial, self.gate)

    @staticmethod
    def _seed_state_dir_if_migrating(config: Config) -> None:
        """Preserves ``active-link-id`` when migrating from ``default`` to a named titella."""
        state_dir = config.ensure_state_dir()
        link_file = state_dir / "active-link-id"
        if link_file.exists():
            return
        parent = state_dir.parent
        if parent.name == "titellas":
            root_link = parent.parent / "active-link-id"
            if root_link.is_file():
                try:
                    value = root_link.read_text(encoding="ascii").strip()
                    if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
                        link_file.write_text(value, encoding="ascii")
                        link_file.chmod(0o600)
                except OSError:
                    pass

    def _on_agent_link_state(self, state: str) -> None:
        if state in {"agent_unreachable", "disconnected", "closed"}:
            self.gate.set_ready(False)

    def _bind_live_agent(self) -> None:
        try:
            response = self.serial.request(
                "control", "bind", {"link_id": self.serial.link_id}, timeout=10
            )
        except TransportError:
            return
        finally:
            with self._bind_lock:
                self._binding = False
        body = dict(response.get("body") or {})
        if body.get("status") == "ready":
            self.gate.set_ready(True)

    def _attach(self, frame: dict[str, Any]) -> bool:
        """A live agent binds itself on hello or heartbeat whenever the gate is closed."""
        if frame.get("type") not in {"hello", "heartbeat"} or self.gate.ready:
            return True
        if self.bootstrap.status().get("status") not in {
            "not_started", "error", "stopped", "ready"
        }:
            return True
        with self._bind_lock:
            if self._binding:
                return True
            self._binding = True
        threading.Thread(
            target=self._bind_live_agent,
            name=f"agent-attach-{self.titella_id}",
            daemon=True,
        ).start()
        return True

    def _reattach(self) -> None:
        if not self.serial.wait_connected(5) or self.gate.ready:
            return
        with self._bind_lock:
            if self._binding:
                return
            self._binding = True
        self._bind_live_agent()

    def reload_non_transport(self, new_config: Config) -> None:
        """Updates screen/launcher/platform fields without closing the transport."""
        self.config = new_config
        self.capture.config = new_config
        self.bootstrap.config = new_config

    def close(self, *, clean: bool = False) -> None:
        self.shells.close()
        self._unsubscribe_agent_state()
        self.files.close()
        self.serial.close(clean=clean)

    def agent_summary(self) -> dict[str, Any]:
        state = self.bootstrap.status()
        if not self.gate.ready and state.get("status") == "ready":
            state = {
                **state,
                "status": "error",
                "error": {
                    "code": "agent_not_ready",
                    "message": "the slave agent is not reachable",
                    "retryable": True,
                },
            }
        elif self.gate.ready and state.get("status") != "ready":
            state = {**state, "status": "ready"}
            state.pop("error", None)
        return state

    def kvm_status(self) -> dict[str, Any]:
        try:
            snapshot = self.firmware.snapshot()
        except BaseException as exc:
            snapshot = {
                "reachable": False,
                "error": {"code": "internal_error", "message": str(exc)},
            }
        report = diagnose(
            self.config,
            firmware=snapshot,
            lease=self.lease.status(),
            agent=self.agent_summary(),
            resolution=self.capture.resolution(),
            capture_age_s=self.capture.age_s(),
        )
        report["titella"] = self.titella_id
        return report

    def summary(self, *, is_default: bool) -> dict[str, Any]:
        return {
            "titella": self.titella_id,
            "default": is_default,
            "transport": self.config.transport,
            "serial_port": self.config.serial_port,
            "serial_baud": self.config.serial_baud,
            "wifi_host": self.config.wifi_host,
            "wifi_port": self.config.wifi_port,
            "screen_device": self.config.screen_device,
            "screen_input_format": self.config.screen_input_format,
            "screen_pixel_format": self.config.screen_pixel_format,
            "screen_framerate": self.config.screen_framerate,
            "keyboard_layout": self.config.keyboard_layout,
            "slave_platform": self.config.slave_platform,
            "slave_cdc_port": self.config.slave_cdc_port,
            "terminal_mods": list(self.config.terminal_mods) if self.config.terminal_mods else None,
            "terminal_key": self.config.terminal_key,
            "launcher_text": self.config.launcher_text,
            "connected": self.serial.connected,
            "link_state": getattr(self.serial, "state", "idle"),
            "agent_ready": self.gate.ready,
            "agent": self.agent_summary(),
            "lease": self.lease.status(),
        }


def _transport_key(config: Config) -> tuple[Any, ...]:
    return (
        config.transport,
        config.serial_port,
        config.serial_baud,
        config.wifi_host,
        config.wifi_port,
        config.heartbeat_s,
        config.disconnect_grace_s,
    )


class TitellaRegistry:
    """Thread-safe registry of configured ``[titella.<id>]`` instances."""

    def __init__(
        self,
        config: Config | None = None,
        link: SerialLink | None = None,
        firmware: FirmwareInfo | None = None,
        instance_factory: Callable[[Config], TitellaInstance] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.root_config = config or load_config()
        self._factory = instance_factory or (lambda cfg: TitellaInstance(cfg))
        self.default_titella: str | None = self.root_config.default_titella
        self._instances: dict[str, TitellaInstance] = {}

        configured = dict(self.root_config.titellas)
        if not configured:
            tid = self.root_config.titella_id or DEFAULT_TITELLA_ID
            configured = {tid: self.root_config}
            self.default_titella = tid

        if link is not None or firmware is not None:
            primary_id = self.default_titella or next(iter(configured))
            primary_cfg = configured[primary_id]
            self._instances[primary_id] = TitellaInstance(
                primary_cfg, link=link, firmware=firmware
            )
            for tid, cfg in configured.items():
                if tid != primary_id:
                    self._instances[tid] = self._factory(cfg)
        else:
            for tid, cfg in configured.items():
                self._instances[tid] = self._factory(cfg)

    def resolve(self, titella: str | None = None) -> TitellaInstance:
        with self._lock:
            if titella is not None and str(titella).strip():
                tid = str(titella).strip()
                instance = self._instances.get(tid)
                if instance is None:
                    available = ", ".join(sorted(self._instances)) or "none"
                    raise TransportError(
                        "titella_not_found",
                        f"unknown titella {tid!r}; registered: {available}",
                        False,
                    )
                return instance
            if self.default_titella and self.default_titella in self._instances:
                return self._instances[self.default_titella]
            if len(self._instances) == 1:
                return next(iter(self._instances.values()))
            available = ", ".join(sorted(self._instances))
            raise TransportError(
                "titella_required",
                f"multiple titellas are registered ({available}); pass titella=<id> or call titella_set_default",
                False,
            )

    def list_titellas(self) -> dict[str, Any]:
        with self._lock:
            entries = [
                instance.summary(is_default=(tid == self.default_titella))
                for tid, instance in sorted(self._instances.items())
            ]
            return {
                "ok": True,
                "default_titella": self.default_titella,
                "config_path": str(self.root_config.config_path),
                "titellas": entries,
            }

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            instances = dict(self._instances)
            default_id = self.default_titella
        titellas_health: dict[str, Any] = {}
        for tid, instance in sorted(instances.items()):
            try:
                snap = instance.firmware.snapshot()
            except BaseException as exc:
                snap = {
                    "reachable": False,
                    "error": {"code": "internal_error", "message": str(exc)},
                }
            titellas_health[tid] = {
                "default": tid == default_id,
                "agent_ready": instance.gate.ready,
                "firmware": snap,
            }
        primary_id = default_id or (next(iter(titellas_health)) if titellas_health else None)
        primary_fw = (
            titellas_health[primary_id]["firmware"]
            if primary_id and primary_id in titellas_health
            else {"reachable": False}
        )
        return {
            "default_titella": default_id,
            "firmware": primary_fw,
            "titellas": titellas_health,
        }

    def close_all(self) -> None:
        with self._lock:
            instances = list(self._instances.values())
        for instance in instances:
            instance.close(clean=False)

    def _read_persisted_model(
        self,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str | None]:
        raw = _read_raw_toml(self.root_config.config_path)
        globals_map, named = _split_document(raw)
        titellas_map: dict[str, dict[str, Any]] = {
            tid: dict(table) for tid, table in named.items()
        }
        default_id = globals_map.get("default_titella") or self.default_titella
        if isinstance(default_id, str):
            default_id = default_id.strip() or None
        else:
            default_id = None

        # If the file was flat (no [titella.<id>] yet), materialize the existing
        # single instance as a table when mutating the registry so multi-titella
        # edits preserve the prior rig configuration cleanly.
        if not titellas_map and self._instances:
            for tid, inst in self._instances.items():
                table: dict[str, Any] = {
                    "transport": inst.config.transport,
                    "serial_baud": inst.config.serial_baud,
                    "wifi_host": inst.config.wifi_host,
                    "wifi_port": inst.config.wifi_port,
                    "screen_device": inst.config.screen_device,
                    "screen_pixel_format": inst.config.screen_pixel_format,
                    "screen_framerate": inst.config.screen_framerate,
                    "keyboard_layout": inst.config.keyboard_layout,
                }
                if inst.config.serial_port:
                    table["serial_port"] = inst.config.serial_port
                if inst.config.screen_input_format:
                    table["screen_input_format"] = inst.config.screen_input_format
                if inst.config.slave_platform:
                    table["slave_platform"] = inst.config.slave_platform
                if inst.config.slave_cdc_port:
                    table["slave_cdc_port"] = inst.config.slave_cdc_port
                if inst.config.terminal_mods is not None:
                    table["terminal_mods"] = list(inst.config.terminal_mods)
                if inst.config.terminal_key is not None:
                    table["terminal_key"] = inst.config.terminal_key
                if inst.config.launcher_text is not None:
                    table["launcher_text"] = inst.config.launcher_text
                titellas_map[tid] = table
            for key in list(globals_map):
                if key in _INSTANCE_KEYS and key not in {"heartbeat_s", "disconnect_grace_s"}:
                    globals_map.pop(key, None)
        if "state_dir" not in globals_map:
            globals_map["state_dir"] = str(self.root_config.state_dir)
        if "http_port" not in globals_map:
            globals_map["http_port"] = self.root_config.http_port
        return globals_map, titellas_map, default_id

    def upsert(
        self,
        titella: str,
        *,
        transport: str | None = None,
        serial_port: str | None = None,
        serial_baud: int | None = None,
        wifi_host: str | None = None,
        wifi_port: int | None = None,
        screen_device: str | None = None,
        screen_input_format: str | None = None,
        screen_pixel_format: str | None = None,
        screen_framerate: int | None = None,
        keyboard_layout: str | None = None,
        slave_platform: str | None = None,
        slave_cdc_port: str | None = None,
        terminal_mods: list[str] | str | None = None,
        terminal_key: str | None = None,
        launcher_text: str | None = None,
        set_default: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        tid = validate_titella_id(titella)
        with self._lock:
            globals_map, titellas_map, default_id = self._read_persisted_model()
            # If the only existing entry is the implicit "default" and the user
            # is registering a named titella pointing at the same serial port,
            # promote "default" in place so the live link and agent are kept.
            existing = self._instances.get(tid)
            promoted_from_default = False
            if (
                existing is None
                and tid != DEFAULT_TITELLA_ID
                and set(self._instances) == {DEFAULT_TITELLA_ID}
            ):
                def_inst = self._instances[DEFAULT_TITELLA_ID]
                target_port = serial_port or def_inst.config.serial_port
                if target_port and target_port == def_inst.config.serial_port:
                    existing = def_inst
                    promoted_from_default = True
                    titellas_map[tid] = dict(titellas_map.pop(DEFAULT_TITELLA_ID, {}))
                    if default_id == DEFAULT_TITELLA_ID:
                        default_id = tid

            current_table = dict(titellas_map.get(tid, {}))
            updates: dict[str, Any] = {
                "transport": transport,
                "serial_port": serial_port,
                "serial_baud": serial_baud,
                "wifi_host": wifi_host,
                "wifi_port": wifi_port,
                "screen_device": screen_device,
                "screen_input_format": screen_input_format,
                "screen_pixel_format": screen_pixel_format,
                "screen_framerate": screen_framerate,
                "keyboard_layout": keyboard_layout,
                "slave_platform": slave_platform,
                "slave_cdc_port": slave_cdc_port,
                "terminal_mods": terminal_mods,
                "terminal_key": terminal_key,
                "launcher_text": launcher_text,
            }
            for key, value in updates.items():
                if value is not None:
                    current_table[key] = value
            titellas_map[tid] = current_table

            if set_default or default_id is None or len(titellas_map) == 1:
                default_id = tid

            # Validate by writing and loading through load_config.
            save_titella_config(
                self.root_config.config_path,
                global_values=globals_map,
                titellas=titellas_map,
                default_titella=default_id,
            )
            reloaded_root = load_config(self.root_config.config_path, environ={})
            new_cfg = reloaded_root.titellas[tid]

            if existing is not None:
                transport_changed = _transport_key(existing.config) != _transport_key(new_cfg)
                if transport_changed and existing.gate.ready and not force:
                    raise TransportError(
                        "titella_busy",
                        f"titella {tid!r} has a live slave agent; pass force=True or stop the agent before changing its transport/port",
                        False,
                    )
                if promoted_from_default:
                    self._instances.pop(DEFAULT_TITELLA_ID, None)
                    existing.titella_id = tid
                    TitellaInstance._seed_state_dir_if_migrating(new_cfg)
                    existing.lease = HidLease(new_cfg)
                    existing.hid.lease = existing.lease
                    self._instances[tid] = existing
                if transport_changed:
                    existing.close(clean=False)
                    self._instances[tid] = self._factory(new_cfg)
                else:
                    existing.reload_non_transport(new_cfg)
            else:
                self._instances[tid] = self._factory(new_cfg)

            self.root_config = reloaded_root
            self.default_titella = reloaded_root.default_titella
            instance = self._instances[tid]
            return {
                "ok": True,
                "status": "updated" if existing is not None else "created",
                "default_titella": self.default_titella,
                "titella": instance.summary(is_default=(tid == self.default_titella)),
            }

    def remove(self, titella: str, *, force: bool = False) -> dict[str, Any]:
        tid = validate_titella_id(titella)
        with self._lock:
            instance = self._instances.get(tid)
            if instance is None:
                raise TransportError(
                    "titella_not_found",
                    f"unknown titella {tid!r}",
                    False,
                )
            if len(self._instances) <= 1:
                raise TransportError(
                    "invalid_argument",
                    "cannot remove the last registered titella",
                    False,
                )
            if instance.gate.ready and not force:
                raise TransportError(
                    "titella_busy",
                    f"titella {tid!r} has a live slave agent; pass force=True or call slave_stop first",
                    False,
                )
            globals_map, titellas_map, default_id = self._read_persisted_model()
            titellas_map.pop(tid, None)
            if default_id == tid:
                default_id = next(iter(sorted(titellas_map)), None)
            save_titella_config(
                self.root_config.config_path,
                global_values=globals_map,
                titellas=titellas_map,
                default_titella=default_id,
            )
            instance.close(clean=False)
            self._instances.pop(tid, None)
            self.root_config = load_config(self.root_config.config_path, environ={})
            self.default_titella = default_id
            return {
                "ok": True,
                "status": "removed",
                "removed": tid,
                "default_titella": self.default_titella,
                "remaining": sorted(self._instances),
            }

    def set_default(self, titella: str) -> dict[str, Any]:
        tid = validate_titella_id(titella)
        with self._lock:
            if tid not in self._instances:
                available = ", ".join(sorted(self._instances))
                raise TransportError(
                    "titella_not_found",
                    f"unknown titella {tid!r}; registered: {available}",
                    False,
                )
            globals_map, titellas_map, _ = self._read_persisted_model()
            save_titella_config(
                self.root_config.config_path,
                global_values=globals_map,
                titellas=titellas_map,
                default_titella=tid,
            )
            self.default_titella = tid
            self.root_config = replace(self.root_config, default_titella=tid)
            return {
                "ok": True,
                "status": "default_updated",
                "default_titella": tid,
            }

    def discover(self) -> dict[str, Any]:
        """Discovers candidate serial ports and local UVC capture devices safely.

        Never opens or writes to an unknown serial port: it inspects USB VID/PID
        metadata only (via pyserial ``list_ports`` when available or ``ioreg`` /
        ``/sys/class/tty``) and cross-references already registered titellas.
        """
        with self._lock:
            registered_by_port = {
                inst.config.serial_port: tid
                for tid, inst in self._instances.items()
                if inst.config.serial_port
            }
            registered_screens = {
                inst.config.screen_device: tid
                for tid, inst in self._instances.items()
                if inst.config.screen_device
            }
        serial_candidates = _discover_serial_ports(registered_by_port)
        video_devices = _discover_video_devices(registered_screens)
        return {
            "ok": True,
            "serial_ports": serial_candidates,
            "video_devices": video_devices,
        }


def _discover_serial_ports(registered_by_port: Mapping[str, str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        import serial.tools.list_ports as list_ports  # type: ignore[import-not-found]
    except Exception:
        list_ports = None

    if list_ports is not None:
        for info in sorted(list_ports.comports(), key=lambda item: item.device or ""):
            if info.vid not in KNOWN_USB_VIDS:
                continue
            device = str(info.device)
            seen.add(device)
            results.append({
                "port": device,
                "vid": f"0x{info.vid:04x}" if info.vid is not None else None,
                "pid": f"0x{info.pid:04x}" if info.pid is not None else None,
                "manufacturer": info.manufacturer,
                "product": info.product or info.description,
                "serial_number": info.serial_number,
                "registered_titella": registered_by_port.get(device),
            })

    if not results and sys.platform == "darwin":
        ioreg_meta = _darwin_ioreg_serial_metadata()
        for pattern in ("/dev/cu.wchusbserial*", "/dev/cu.usbserial*", "/dev/cu.usbmodem*"):
            for path in sorted(Path("/dev").glob(pattern.removeprefix("/dev/"))):
                device = str(path)
                if device in seen:
                    continue
                seen.add(device)
                meta = ioreg_meta.get(device, {})
                results.append({
                    "port": device,
                    "vid": meta.get("vid"),
                    "pid": meta.get("pid"),
                    "manufacturer": meta.get("manufacturer"),
                    "product": meta.get("product"),
                    "serial_number": meta.get("serial_number"),
                    "registered_titella": registered_by_port.get(device),
                })
    elif not results and sys.platform.startswith("linux"):
        for pattern in ("ttyACM*", "ttyUSB*"):
            for path in sorted(Path("/dev").glob(pattern)):
                device = str(path)
                if device in seen:
                    continue
                seen.add(device)
                results.append({
                    "port": device,
                    "vid": None,
                    "pid": None,
                    "manufacturer": None,
                    "product": None,
                    "serial_number": None,
                    "registered_titella": registered_by_port.get(device),
                })
    return results


_AVFOUNDATION_DEVICE_RE = re.compile(r"\[(\d+)\]\s+(.+)$")


def _darwin_ioreg_serial_metadata() -> dict[str, dict[str, str | None]]:
    """Maps ``/dev/cu.*`` callout devices to their closest USB device metadata via ``ioreg``."""
    try:
        completed = subprocess.run(
            ["/usr/sbin/ioreg", "-p", "IOService", "-w0", "-l"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    lines = (completed.stdout or "").splitlines()
    mapping: dict[str, dict[str, str | None]] = {}
    for idx, line in enumerate(lines):
        match = re.search(r'"IOCalloutDevice"\s*=\s*"([^"]+)"', line)
        if not match:
            continue
        device = match.group(1)
        chunk = lines[max(0, idx - 220):idx + 1]
        vid_idx = next(
            (j for j in range(len(chunk) - 1, -1, -1) if '"idVendor" =' in chunk[j]),
            None,
        )
        if vid_idx is None:
            continue
        start = vid_idx
        while start > 0 and chunk[start].lstrip(" |\t") != "{":
            start -= 1
        end = vid_idx
        while end < len(chunk) and chunk[end].lstrip(" |\t") != "}":
            end += 1
        block = "\n".join(chunk[start:end + 1])
        vid_m = re.search(r'"idVendor"\s*=\s*(\d+)', block)
        pid_m = re.search(r'"idProduct"\s*=\s*(\d+)', block)
        if not vid_m or not pid_m:
            continue
        prod_m = re.search(r'"USB Product Name"\s*=\s*"([^"]+)"', block)
        vend_m = re.search(r'"USB Vendor Name"\s*=\s*"([^"]+)"', block)
        sn_m = re.search(r'"USB Serial Number"\s*=\s*"([^"]+)"', block)
        mapping[device] = {
            "vid": f"0x{int(vid_m.group(1)):04x}",
            "pid": f"0x{int(pid_m.group(1)):04x}",
            "manufacturer": vend_m.group(1) if vend_m else None,
            "product": prod_m.group(1) if prod_m else None,
            "serial_number": sn_m.group(1) if sn_m else None,
        }
    return mapping


def _discover_video_devices(registered_screens: Mapping[str, str]) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    if sys.platform == "darwin" and shutil.which("ffmpeg"):
        try:
            completed = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            in_video = False
            for line in (completed.stderr or "").splitlines():
                if "AVFoundation video devices" in line:
                    in_video = True
                    continue
                if "AVFoundation audio devices" in line:
                    break
                if in_video:
                    match = _AVFOUNDATION_DEVICE_RE.search(line)
                    if match:
                        index, name = match.group(1), match.group(2).strip()
                        devices.append({
                            "index": int(index),
                            "name": name,
                            "backend": "avfoundation",
                            "registered_titella": registered_screens.get(name) or registered_screens.get(index),
                        })
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif sys.platform.startswith("linux"):
        for node in sorted(Path("/dev").glob("video*")):
            device = str(node)
            label_path = Path("/sys/class/video4linux") / node.name / "name"
            try:
                label = label_path.read_text(encoding="utf-8").strip()
            except OSError:
                label = node.name
            devices.append({
                "device": device,
                "name": label,
                "backend": "v4l2",
                "registered_titella": registered_screens.get(device) or registered_screens.get(label),
            })
    return devices
