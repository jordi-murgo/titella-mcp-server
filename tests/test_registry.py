from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from titella.config import Config, load_config
from titella.firmware_info import FirmwareInfo
from titella.registry import TitellaInstance, TitellaRegistry
from titella.serial_link import TransportError


class _FakeLink:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.link_id = "0123456789abcdef0123456789abcdef"
        self.connected = True
        self.state = "connected"
        self.port = config.serial_port or f"{config.wifi_host}:{config.wifi_port}"
        self.transport = config.transport
        self.closed = False
        self.handlers: list[Any] = []
        self.state_handlers: list[Any] = []

    def start(self) -> None:
        return None

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return True

    def subscribe(self, channel: str, handler: Any, session_id: str | None = None):
        self.handlers.append((channel, handler, session_id))
        return lambda: None

    def subscribe_state(self, handler: Any):
        self.state_handlers.append(handler)
        handler(self.state)
        return lambda: self.state_handlers.remove(handler)

    def emit_state(self, state: str) -> None:
        self.state = state
        for handler in list(self.state_handlers):
            handler(state)

    def request(self, channel: str, op: str, body: dict[str, Any] | None = None, timeout: float = 6.0) -> dict[str, Any]:
        if channel == "hid" and op == "health":
            return {"body": {"ok": True, "status": "hid_ready", "firmware": "1.3.0", "protocol": 1, "uart_baud": self.config.serial_baud}}
        if channel == "control" and op == "bind":
            return {"body": {"status": "ready", "link_id": self.link_id}}
        return {"body": {"ok": True}}

    def close(self, clean: bool = True) -> None:
        self.closed = True


def _fake_instance(config: Config) -> TitellaInstance:
    link = _FakeLink(config)
    return TitellaInstance(config, link=link, firmware=FirmwareInfo(link))  # type: ignore[arg-type]


class RegistryTests(unittest.TestCase):
    def test_multiple_titellas_without_default_require_explicit_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                f'state_dir = "{root / "state"}"\n\n'
                '[titella.alpha]\nserial_port = "/dev/cu.alpha"\n\n'
                '[titella.beta]\nserial_port = "/dev/cu.beta"\n',
                encoding="utf-8",
            )
            config = load_config(cfg_path, {})
            self.assertIsNone(config.default_titella)
            reg = TitellaRegistry(config, instance_factory=_fake_instance)

            with self.assertRaises(TransportError) as ctx:
                reg.resolve(None)
            self.assertEqual(ctx.exception.code, "titella_required")

            self.assertEqual(reg.resolve("alpha").titella_id, "alpha")
            self.assertEqual(reg.resolve("beta").titella_id, "beta")

            reg.set_default("beta")
            self.assertEqual(reg.resolve(None).titella_id, "beta")

    def test_upsert_promotes_default_and_hot_reloads_without_closing_other_titellas(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg_path = root / "config.toml"
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            (state_dir / "active-link-id").write_text("ea3371ef2a20258d5e4e6b79816d0dd9", encoding="ascii")
            cfg_path.write_text(
                f'state_dir = "{state_dir}"\n'
                'serial_port = "/dev/cu.wch1"\n'
                'serial_baud = 2000000\n'
                'slave_platform = "linux"\n'
                'terminal_mods = "CMD"\n'
                'terminal_key = "RETURN"\n',
                encoding="utf-8",
            )
            config = load_config(cfg_path, {})
            reg = TitellaRegistry(config, instance_factory=_fake_instance)
            default_inst = reg.resolve()
            default_inst.gate.set_ready(True)

            # Promoting the single flat "default" to "omarchy" on the same port
            # keeps the live instance and its ready gate untouched.
            promoted = reg.upsert("omarchy", slave_platform="linux", set_default=True)
            self.assertEqual(promoted["status"], "updated")
            self.assertEqual(promoted["default_titella"], "omarchy")
            omarchy_inst = reg.resolve("omarchy")
            self.assertIs(omarchy_inst, default_inst)
            self.assertTrue(omarchy_inst.gate.ready)
            self.assertFalse(omarchy_inst.serial.closed)  # type: ignore[attr-defined]

            # Adding a second titella over TCP leaves omarchy untouched.
            created = reg.upsert(
                "winlab",
                transport="wifi",
                wifi_host="10.0.0.50",
                wifi_port=5555,
                slave_platform="windows",
            )
            self.assertEqual(created["status"], "created")
            self.assertFalse(omarchy_inst.serial.closed)  # type: ignore[attr-defined]
            self.assertEqual(reg.resolve("winlab").config.wifi_host, "10.0.0.50")

            # Changing transport on a busy titella without force=True is refused.
            with self.assertRaises(TransportError) as busy_ctx:
                reg.upsert("omarchy", serial_port="/dev/cu.other")
            self.assertEqual(busy_ctx.exception.code, "titella_busy")

            # Removing the non-busy winlab succeeds; removing the last titella is refused.
            reg.resolve("winlab").gate.set_ready(False)
            removed = reg.remove("winlab")
            self.assertEqual(removed["removed"], "winlab")
            with self.assertRaises(TransportError) as last_ctx:
                reg.remove("omarchy", force=True)
            self.assertEqual(last_ctx.exception.code, "invalid_argument")

    def test_agent_route_failure_closes_stale_gate_and_rebinds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(
                Path(temp) / "absent.toml",
                {"TITELLA_STATE_DIR": str(Path(temp) / "state")},
            )
            instance = _fake_instance(config)
            link = instance.serial
            instance.bootstrap._set(status="ready", session_id="old")
            instance.gate.set_ready(True)

            link.emit_state("agent_unreachable")  # type: ignore[attr-defined]

            self.assertFalse(instance.gate.ready)
            summary = instance.agent_summary()
            self.assertEqual(summary["status"], "error")
            self.assertEqual(summary["error"]["code"], "agent_not_ready")

            instance._bind_live_agent()
            self.assertTrue(instance.gate.ready)
            instance.close()


if __name__ == "__main__":
    unittest.main()
