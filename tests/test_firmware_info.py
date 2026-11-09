from __future__ import annotations

import unittest
from typing import Any

from titella.firmware_info import FirmwareInfo
from titella.serial_link import TransportError


class FakeLink:
    def __init__(self, body: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.body = body or {}
        self.error = error
        self.connected = True
        self.port: str | None = "/dev/cu.fake"
        self.calls = 0

    def start(self) -> None:
        return None

    def request(self, channel: str, op: str, body: dict[str, Any], timeout: float = 6.0) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"body": self.body}


class FirmwareInfoTests(unittest.TestCase):
    def test_reports_firmware_and_protocol_from_health(self) -> None:
        link = FakeLink({"ok": True, "status": "hid_ready", "firmware": "1.2.0", "protocol": 1})
        snapshot = FirmwareInfo(link).snapshot()  # type: ignore[arg-type]
        self.assertTrue(snapshot["reachable"])
        self.assertEqual(snapshot["firmware"], "1.2.0")
        self.assertEqual(snapshot["protocol"], 1)
        self.assertEqual(snapshot["port"], "/dev/cu.fake")

    def test_forwards_configured_uart_baud_from_health(self) -> None:
        link = FakeLink({"firmware": "1.2.2", "protocol": 1, "uart_baud": 2000000})
        snapshot = FirmwareInfo(link).snapshot()  # type: ignore[arg-type]
        self.assertEqual(snapshot["uart_baud"], 2000000)

    def test_forwards_wifi_state_from_health(self) -> None:
        wifi = {"enabled": True, "ssid": "Titella",
                "ip": "192.168.4.1", "tcp_client": True}
        link = FakeLink({"firmware": "1.3.0", "protocol": 1, "wifi": wifi})
        snapshot = FirmwareInfo(link).snapshot()  # type: ignore[arg-type]
        self.assertEqual(snapshot["wifi"], wifi)
        self.assertEqual(snapshot["transport"], "serial")

    def test_reports_wifi_transport_of_the_link(self) -> None:
        link = FakeLink({"firmware": "1.3.0", "protocol": 1})
        link.transport = "wifi"  # type: ignore[attr-defined]
        self.assertEqual(
            FirmwareInfo(link).snapshot()["transport"],  # type: ignore[arg-type]
            "wifi",
        )

    def test_caches_so_health_does_not_poll_the_uart_on_every_request(self) -> None:
        link = FakeLink({"firmware": "1.2.0", "protocol": 1})
        info = FirmwareInfo(link)  # type: ignore[arg-type]
        first = info.snapshot()
        second = info.snapshot()
        self.assertEqual(first, second)
        self.assertEqual(link.calls, 1)

    def test_unreachable_transport_is_reported_without_raising(self) -> None:
        link = FakeLink(error=TransportError("request_timeout", "timeout en hid.health", True))
        snapshot = FirmwareInfo(link).snapshot()  # type: ignore[arg-type]
        self.assertFalse(snapshot["reachable"])
        self.assertEqual(snapshot["error"]["code"], "request_timeout")

    def test_starts_the_link_so_health_can_reflect_the_firmware(self) -> None:
        link = FakeLink({"firmware": "1.2.0", "protocol": 1})
        link.started = 0
        link.start = lambda: setattr(link, "started", link.started + 1)  # type: ignore[method-assign]
        FirmwareInfo(link).snapshot()  # type: ignore[arg-type]
        self.assertEqual(link.started, 1)


if __name__ == "__main__":
    unittest.main()
