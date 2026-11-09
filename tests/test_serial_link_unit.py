from __future__ import annotations

import os
import struct
import tempfile
import termios
import unittest
from dataclasses import replace

from titella.config import load_config
from titella.serial_link import SerialLink, TransportError
from titella_slave_assets.wire import base_frame, data_b64


class QueueOnlyLink(SerialLink):
    def start(self) -> None:
        self._started.set()


class AckLink(QueueOnlyLink):
    def __init__(self, config, acknowledge_on: int | None):
        super().__init__(config)
        self.acknowledge_on = acknowledge_on
        self.sent = 0

    def send_frame(self, frame):
        self.sent += 1
        if self.acknowledge_on == self.sent:
            future = self._acks[(frame["session_id"], frame["seq"])]
            future.set_result(None)


class SerialLinkUnitTests(unittest.TestCase):
    def config(self, temp: str):
        # Explicit path to a nonexistent TOML: the operator's real file
        # (~/.config/titella/config.toml) must not leak in.
        return replace(
            load_config(
                os.path.join(temp, "absent.toml"),
                {"TITELLA_STATE_DIR": temp, "TITELLA_HEARTBEAT_S": "1", "TITELLA_DISCONNECT_GRACE_S": "1"},
            ),
            ack_timeout_s=0.01,
            ack_retries=3,
        )

    def test_priority_queue_is_stable_and_bounds_stream_latency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = QueueOnlyLink(self.config(temp))
            frames = [
                base_frame(link.link_id, "transfer", "data", session_id="t", seq=0, data_b64=data_b64(b"t")),
                base_frame(link.link_id, "shell", "data", session_id="s", seq=0, data_b64=data_b64(b"s")),
                base_frame(link.link_id, "shell", "request", request_id=1, op="terminal.resize"),
                base_frame(link.link_id, "hid", "request", request_id=2, op="mouse_rel"),
                base_frame(link.link_id, "shell", "ack", session_id="s", ack_seq=0),
                base_frame(link.link_id, "hid", "request", request_id=3, op="mouse_wheel"),
            ]
            for frame in frames:
                link.send_frame(frame)
            ordered = [link._outbound.get_nowait()[2] for _ in frames]
            self.assertEqual(
                [(frame["channel"], frame["type"], frame.get("request_id")) for frame in ordered],
                [
                    ("hid", "request", 2),
                    ("shell", "ack", None),
                    ("hid", "request", 3),
                    ("shell", "request", 1),
                    ("shell", "data", None),
                    ("transfer", "data", None),
                ],
            )
            link.close()

    def test_agent_route_error_marks_agent_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = QueueOnlyLink(self.config(temp))
            states: list[str] = []
            link.subscribe_state(states.append)
            link._dispatch(base_frame(
                link.link_id,
                "shell",
                "error",
                request_id=7,
                op="job.start",
                body={
                    "code": "route_error",
                    "message": "short write while forwarding",
                    "retryable": False,
                },
            ))
            self.assertEqual(states[-1], "agent_unreachable")
            link.close()

    def test_open_releases_modem_lines_and_tolerates_unsupported_ioctl(self) -> None:
        """Opening a port must not leave the ESP32 in reset (DTR+RTS asserted)."""
        import os
        import pty
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "titella.serial_link.fcntl.ioctl"
        ) as ioctl:
            master, slave = pty.openpty()
            self.addCleanup(lambda: os.close(master) if master >= 0 else None)
            port = Path(temp) / "serial"
            port.symlink_to(os.ttyname(slave))
            os.close(slave)
            config = replace(self.config(temp), serial_port=str(port))

            link = QueueOnlyLink(config)
            link._open()
            self.assertTrue(link.connected)
            self.assertIsNotNone(link._fd)
            clear_calls = [
                call for call in ioctl.call_args_list
                if len(call.args) >= 2 and call.args[1] == termios.TIOCMBIC
            ]
            self.assertEqual(len(clear_calls), 1)
            self.assertEqual(
                struct.unpack("I", clear_calls[0].args[2])[0],
                termios.TIOCM_DTR | termios.TIOCM_RTS,
            )
            link._close_fd()

            # PTYs lack modem control: the error must not abort.
            ioctl.reset_mock()
            ioctl.side_effect = lambda *args: (
                (_ for _ in ()).throw(OSError(25, "Inappropriate ioctl for device"))
                if args[1] == termios.TIOCMBIC else None
            )
            second = QueueOnlyLink(config)
            second._open()
            self.assertTrue(second.connected)
            second._close_fd()

    def test_custom_baud_uses_iossiospeed_after_termios(self) -> None:
        """A non-traditional speed is applied with IOSSIOSPEED, not B115200."""
        import os
        import pty
        from pathlib import Path
        from unittest import mock

        from titella.serial_link import _IOSSIOSPEED

        baud = 1234567
        self.assertIsNone(getattr(termios, f"B{baud}", None), "rate must be nontraditional")
        with tempfile.TemporaryDirectory() as temp:
            master, slave = pty.openpty()
            self.addCleanup(lambda: os.close(master) if master >= 0 else None)
            port = Path(temp) / "serial"
            port.symlink_to(os.ttyname(slave))
            os.close(slave)
            config = replace(self.config(temp), serial_port=str(port), serial_baud=baud)
            events: list[tuple[str, object]] = []
            real_tcsetattr = termios.tcsetattr

            def record_tcsetattr(fd, when, attrs):
                events.append(("tcsetattr", when))
                return real_tcsetattr(fd, when, attrs)

            with mock.patch(
                "titella.serial_link.sys.platform", "darwin"
            ), mock.patch(
                "titella.serial_link.termios.tcsetattr", record_tcsetattr
            ), mock.patch(
                "titella.serial_link.fcntl.ioctl"
            ) as ioctl:
                ioctl.side_effect = lambda *args: events.append(("ioctl", args[1]))
                link = QueueOnlyLink(config)
                link._open()
                link._close_fd()

            speed_calls = [call for call in ioctl.call_args_list if call.args[1] == _IOSSIOSPEED]
            self.assertEqual(len(speed_calls), 1)
            self.assertEqual(speed_calls[0].args[2], struct.pack("I", baud))
            order = [kind for kind, value in events
                     if kind == "tcsetattr" or value == _IOSSIOSPEED]
            self.assertEqual(order, ["tcsetattr", "ioctl"], "IOSSIOSPEED comes after termios")
            # Releasing DTR/RTS still happens after setting the speed.
            after_speed = [value for kind, value in events if kind == "ioctl"]
            self.assertEqual(after_speed[-1], termios.TIOCMBIC)

    def test_traditional_baud_keeps_termios_constant(self) -> None:
        """115200 keeps using B115200 and never calls IOSSIOSPEED."""
        import os
        import pty
        from pathlib import Path
        from unittest import mock

        from titella.serial_link import _IOSSIOSPEED

        with tempfile.TemporaryDirectory() as temp:
            master, slave = pty.openpty()
            self.addCleanup(lambda: os.close(master) if master >= 0 else None)
            port = Path(temp) / "serial"
            port.symlink_to(os.ttyname(slave))
            os.close(slave)
            config = replace(self.config(temp), serial_port=str(port))
            with mock.patch("titella.serial_link.fcntl.ioctl") as ioctl:
                link = QueueOnlyLink(config)
                link._open()
                link._close_fd()
            self.assertEqual(
                [call for call in ioctl.call_args_list if call.args[1] == _IOSSIOSPEED],
                [],
            )

    def test_rejects_custom_baud_where_platform_has_no_ioctl(self) -> None:
        from unittest import mock

        baud = 123456
        self.assertIsNone(getattr(termios, f"B{baud}", None))
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "titella.serial_link.sys.platform", "linux"
        ):
            with self.assertRaisesRegex(ValueError, "IOSSIOSPEED"):
                QueueOnlyLink(replace(self.config(temp), serial_baud=baud))

    def test_wifi_transport_ignores_serial_baud_validation(self) -> None:
        """Over Wi-Fi baud rates do not exist: they must not block the link."""
        from unittest import mock

        baud = 123456
        self.assertIsNone(getattr(termios, f"B{baud}", None))
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "titella.serial_link.sys.platform", "linux"
        ):
            link = QueueOnlyLink(
                replace(self.config(temp), transport="wifi", serial_baud=baud)
            )
            self.assertEqual(link.transport, "wifi")
            link.close()

    def test_wifi_transport_connects_and_correlates_over_tcp(self) -> None:
        import socket
        import threading

        from titella_slave_assets.wire import (
            FIRMWARE_TO_MASTER,
            MASTER_TO_FIRMWARE,
            FrameParser,
            encode_frame,
        )

        with tempfile.TemporaryDirectory() as temp:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            host, port = listener.getsockname()
            self.addCleanup(listener.close)

            link = SerialLink(
                replace(
                    self.config(temp),
                    transport="wifi",
                    wifi_host=host,
                    wifi_port=port,
                    # The test peer answers requests but not heartbeats;
                    # a short grace period would mark the link as disconnected.
                    disconnect_grace_s=10.0,
                )
            )
            self.addCleanup(link.close)

            def answer() -> None:
                connection, _ = listener.accept()
                try:
                    parser = FrameParser(MASTER_TO_FIRMWARE, ignore_invalid=True)
                    while True:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        for frame in parser.feed(chunk):
                            if frame["type"] != "request":
                                continue
                            connection.sendall(encode_frame(base_frame(
                                frame["link_id"], frame["channel"], "response",
                                request_id=frame["request_id"], op=frame["op"],
                                body={"ok": True, "status": "hid_ready"},
                            ), FIRMWARE_TO_MASTER))
                finally:
                    connection.close()

            thread = threading.Thread(target=answer, daemon=True)
            thread.start()
            self.assertTrue(link.wait_connected(3), "it did not connect over TCP")
            self.assertEqual(link.port, f"{host}:{port}")
            response = link.request("hid", "health", {}, timeout=3)
            self.assertEqual(response["body"], {"ok": True, "status": "hid_ready"})
            thread.join(timeout=3)

    def test_ack_loss_retries_three_times_then_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = AckLink(self.config(temp), acknowledge_on=None)
            with self.assertRaises(TransportError) as raised:
                link.send_data("transfer", "upload", 0, b"data")
            self.assertEqual(raised.exception.code, "ack_timeout")
            self.assertEqual(link.sent, 4, "one initial send and three retries")
            link.close()

    def test_lost_ack_then_success_reuses_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            link = AckLink(self.config(temp), acknowledge_on=2)
            link.send_data("shell", "pty", 9, b"input")
            self.assertEqual(link.sent, 2)
            link.close()


if __name__ == "__main__":
    unittest.main()
