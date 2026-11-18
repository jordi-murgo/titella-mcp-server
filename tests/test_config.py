from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from titella.config import load_config


class ConfigTests(unittest.TestCase):
    def test_environment_overrides_selected_toml_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.toml"
            config_path.write_text(
                'screen_device = "HDMI Card"\nscreen_framerate = 60\nkeyboard_layout = "en_US"\nheartbeat_s = 9\nhttp_port = 9000\n',
                encoding="utf-8",
            )
            config = load_config(config_path, {
                "TITELLA_SCREEN_DEVICE": "USB3 Video",
                "TITELLA_STATE_DIR": str(root / "state"),
                "TITELLA_HEARTBEAT_S": "7",
                "TITELLA_HTTP_PORT": "9010",
            })
            self.assertEqual(config.screen_device, "USB3 Video")
            self.assertEqual(config.screen_framerate, 60)
            self.assertEqual(config.keyboard_layout, "en_US")
            self.assertEqual(config.heartbeat_s, 7)
            self.assertEqual(config.disconnect_grace_s, 30)
            self.assertEqual(config.http_port, 9010)

    def test_hid_auditor_config_selects_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "chosen.toml"
            path.write_text('screen_pixel_format = "nv12"\n', encoding="utf-8")
            config = load_config(environ={"TITELLA_CONFIG": str(path)})
            self.assertEqual(config.screen_pixel_format, "nv12")

    def test_state_directory_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            config = load_config(environ={"TITELLA_STATE_DIR": str(state)})
            config.ensure_state_dir()
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)

    def test_serial_baud_env_overrides_toml_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.toml"
            config_path.write_text("serial_baud = 460800\n", encoding="utf-8")
            self.assertEqual(load_config(config_path, {}).serial_baud, 460800)
            self.assertEqual(
                load_config(config_path, {"TITELLA_SERIAL_BAUD": "2000000"}).serial_baud,
                2000000,
            )
            missing = root / "absent.toml"
            self.assertEqual(load_config(missing, {}).serial_baud, 2000000)

    def test_rejects_invalid_serial_baud(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "absent.toml"
            for value in ("0", "-5", "not-a-baud"):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "serial_baud"):
                    load_config(missing, {"TITELLA_SERIAL_BAUD": value})

    def test_rejects_unsupported_layout(self) -> None:
        with self.assertRaisesRegex(ValueError, "keyboard_layout must be es_ES or en_US"):
            load_config(environ={"TITELLA_KEYBOARD_LAYOUT": "fr_FR"})

    def test_rejects_invalid_http_port(self) -> None:
        for value in ("1023", "65536", "not-a-port"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "http_port"):
                load_config(environ={"TITELLA_HTTP_PORT": value})

    def test_transport_defaults_to_serial(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "absent.toml"
            config = load_config(missing, {})
            self.assertEqual(config.transport, "serial")
            self.assertEqual(config.wifi_host, "192.168.4.1")
            self.assertEqual(config.wifi_port, 5555)

    def test_transport_from_env_and_toml(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.toml"
            config_path.write_text(
                'transport = "wifi"\nwifi_host = "10.0.0.5"\nwifi_port = 6000\n',
                encoding="utf-8",
            )
            from_toml = load_config(config_path, {})
            self.assertEqual(from_toml.transport, "wifi")
            self.assertEqual(from_toml.wifi_host, "10.0.0.5")
            self.assertEqual(from_toml.wifi_port, 6000)
            # The environment wins over TOML, as with the rest of the keys.
            from_env = load_config(config_path, {
                "TITELLA_TRANSPORT": "WiFi",
                "TITELLA_WIFI_HOST": "192.168.4.2",
                "TITELLA_WIFI_PORT": "7000",
            })
            self.assertEqual(from_env.transport, "wifi")
            self.assertEqual(from_env.wifi_host, "192.168.4.2")
            self.assertEqual(from_env.wifi_port, 7000)

    def test_rejects_invalid_transport(self) -> None:
        for value in ("bluetooth", "tcp", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "transport"):
                load_config(environ={"TITELLA_TRANSPORT": value})

    def test_rejects_invalid_wifi_port(self) -> None:
        for value in ("0", "65536", "not-a-port"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "wifi_port"):
                load_config(environ={"TITELLA_WIFI_PORT": value})

    def test_wifi_host_may_not_be_empty_when_transport_is_wifi(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "absent.toml"
            with self.assertRaisesRegex(ValueError, "wifi_host"):
                load_config(missing, {"TITELLA_TRANSPORT": "wifi", "TITELLA_WIFI_HOST": "  "})
            # With the serial transport the host is unused, so an empty host is not an error.
            self.assertEqual(
                load_config(missing, {"TITELLA_WIFI_HOST": "  "}).wifi_host, ""
            )

    def test_slave_platform_and_cdc_port_follow_the_env_toml_default_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.toml"
            config_path.write_text(
                'slave_platform = "linux"\nslave_cdc_port = "/dev/ttyACM0"\n',
                encoding="utf-8",
            )
            from_toml = load_config(config_path, {})
            self.assertEqual(from_toml.slave_platform, "linux")
            self.assertEqual(from_toml.slave_cdc_port, "/dev/ttyACM0")
            from_env = load_config(config_path, {
                "TITELLA_SLAVE_PLATFORM": "Windows",
                "TITELLA_SLAVE_CDC_PORT": "COM7",
            })
            self.assertEqual(from_env.slave_platform, "windows")
            self.assertEqual(from_env.slave_cdc_port, "COM7")
            missing = root / "absent.toml"
            default = load_config(missing, {})
            self.assertIsNone(default.slave_platform)
            self.assertIsNone(default.slave_cdc_port)

    def test_rejects_unknown_slave_platform(self) -> None:
        for value in ("freebsd", "iOS", "linux2"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "slave_platform"):
                load_config(environ={"TITELLA_SLAVE_PLATFORM": value})

    def test_rejects_invalid_windows_cdc_port_during_config_load(self) -> None:
        for value in ("COM0", "com7", "COM", "/dev/ttyACM0"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "slave_cdc_port"):
                load_config(environ={
                    "TITELLA_SLAVE_PLATFORM": "windows",
                    "TITELLA_SLAVE_CDC_PORT": value,
                })

    def test_slave_transport_defaults_to_cdc_and_follows_env_and_toml(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "absent.toml"
            self.assertEqual(load_config(missing, {}).slave_transport, "cdc")
            config_path = root / "config.toml"
            config_path.write_text(
                'slave_platform = "windows"\nslave_transport = "hid"\n',
                encoding="utf-8",
            )
            from_toml = load_config(config_path, {})
            self.assertEqual(from_toml.slave_transport, "hid")
            from_env = load_config(config_path, {
                "TITELLA_SLAVE_TRANSPORT": "CDC",
            })
            self.assertEqual(from_env.slave_transport, "cdc")

    def test_rejects_unknown_slave_transport(self) -> None:
        for value in ("usb", "serial", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "slave_transport"):
                load_config(environ={"TITELLA_SLAVE_TRANSPORT": value})

    def test_hid_transport_rejects_contradictory_configuration(self) -> None:
        # Every contradiction is a configuration error before a connection
        # exists: a HID bootstrap that only discovers it after connecting has
        # already typed at the wrong target.
        cases = (
            ({"TITELLA_SLAVE_TRANSPORT": "hid"}, "slave_platform=windows"),
            ({"TITELLA_SLAVE_TRANSPORT": "hid", "TITELLA_TRANSPORT": "wifi"},
             "transport=serial"),
            ({"TITELLA_SLAVE_TRANSPORT": "hid", "TITELLA_TRANSPORT": "window",
              "TITELLA_WINDOW_APP": "Citrix Viewer"}, "transport=serial"),
            ({"TITELLA_SLAVE_TRANSPORT": "hid", "TITELLA_AGENT_PROTOCOL": "clipboard"},
             "agent_protocol=serial"),
            ({"TITELLA_SLAVE_TRANSPORT": "hid", "TITELLA_SLAVE_PLATFORM": "macos"},
             "slave_platform=windows"),
            ({"TITELLA_SLAVE_TRANSPORT": "hid", "TITELLA_SLAVE_PLATFORM": "windows",
              "TITELLA_SLAVE_CDC_PORT": "COM5"}, "slave_cdc_port"),
        )
        for environ, message in cases:
            with self.subTest(environ=environ):
                with self.assertRaisesRegex(ValueError, message):
                    load_config(environ=environ)

    def test_hid_transport_accepts_the_full_selection(self) -> None:
        config = load_config(environ={
            "TITELLA_SLAVE_TRANSPORT": "hid",
            "TITELLA_SLAVE_PLATFORM": "windows",
        })
        self.assertEqual(config.slave_transport, "hid")
        self.assertEqual(config.transport, "serial")
        self.assertEqual(config.agent_protocol, "serial")
        self.assertIsNone(config.slave_cdc_port)

    def test_slave_implementation_defaults_to_auto_and_is_selectable(self) -> None:
        # An explicit path keeps the operator's own config.toml out of the
        # test: passing environ replaces the environment, so without it the
        # defaults file would be found on disk.
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "absent.toml"
            # A configuration that never mentions the field keeps the
            # historical windows-dotnet default, so upgrading cannot change an
            # existing rig.
            default = load_config(missing, {
                "TITELLA_SLAVE_PLATFORM": "windows",
                "TITELLA_SLAVE_TRANSPORT": "hid",
            })
            self.assertEqual(default.slave_implementation, "auto")
            explicit = load_config(missing, {
                "TITELLA_SLAVE_PLATFORM": "windows",
                "TITELLA_SLAVE_TRANSPORT": "hid",
                "TITELLA_SLAVE_IMPLEMENTATION": "windows-python",
            })
            self.assertEqual(explicit.slave_implementation, "windows-python")
            dll = load_config(missing, {
                "TITELLA_SLAVE_PLATFORM": "windows",
                "TITELLA_SLAVE_TRANSPORT": "hid",
                "TITELLA_SLAVE_IMPLEMENTATION": "windows-dotnet-dll",
            })
            self.assertEqual(dll.slave_implementation, "windows-dotnet-dll")

    def test_slave_implementation_is_validated_against_the_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "absent.toml"
            with self.assertRaisesRegex(ValueError, "slave_implementation"):
                load_config(missing, {
                    "TITELLA_SLAVE_PLATFORM": "windows",
                    "TITELLA_SLAVE_IMPLEMENTATION": "windows-arm",
                })
            # Selecting an implementation without a Windows target would be
            # silently ignored at bootstrap time, so it is refused here.
            with self.assertRaisesRegex(ValueError, "slave_platform=windows"):
                load_config(missing, {
                    "TITELLA_SLAVE_PLATFORM": "linux",
                    "TITELLA_SLAVE_IMPLEMENTATION": "windows-python",
                })
            with self.assertRaisesRegex(ValueError, "slave_platform=windows"):
                load_config(missing, {
                    "TITELLA_SLAVE_PLATFORM": "linux",
                    "TITELLA_SLAVE_IMPLEMENTATION": "windows-dotnet-dll",
                })

    def test_launcher_fields_stay_optional_and_an_empty_text_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "absent.toml"
            unset = load_config(missing, {})
            self.assertIsNone(unset.terminal_mods)
            self.assertIsNone(unset.terminal_key)
            self.assertIsNone(unset.launcher_text)
            empty = load_config(missing, {"TITELLA_LAUNCHER_TEXT": ""})
            self.assertEqual(empty.launcher_text, "")
            configured = load_config(missing, {
                "TITELLA_TERMINAL_MODS": "SUPER, SHIFT",
                "TITELLA_TERMINAL_KEY": "return",
                "TITELLA_LAUNCHER_TEXT": "kitty",
            })
            self.assertEqual(configured.terminal_mods, ("SUPER", "SHIFT"))
            self.assertEqual(configured.terminal_key, "RETURN")
            self.assertEqual(configured.launcher_text, "kitty")

    def test_dead_launcher_env_mappings_are_gone(self) -> None:
        # TITELLA_LAUNCHER_MODS / TITELLA_LAUNCHER_KEY were never read by
        # Config; they must not resurface as a second, silent channel.
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "absent.toml"
            config = load_config(missing, {
                "TITELLA_LAUNCHER_MODS": "ALT",
                "TITELLA_LAUNCHER_KEY": "F5",
            })
            self.assertIsNone(config.terminal_mods)
            self.assertIsNone(config.terminal_key)
            self.assertIsNone(config.launcher_text)

    def test_multi_titella_tables_inherit_globals_and_isolate_state_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.toml"
            config_path.write_text(
                'state_dir = "' + str(root / "state") + '"\n'
                'http_port = 9123\n'
                'default_titella = "omarchy"\n'
                'serial_baud = 2000000\n\n'
                '[titella.omarchy]\n'
                'serial_port = "/dev/cu.wchusbserial1"\n'
                'slave_platform = "linux"\n'
                'terminal_mods = "CMD"\n'
                'terminal_key = "RETURN"\n\n'
                '[titella.winlab]\n'
                'transport = "wifi"\n'
                'wifi_host = "10.0.0.21"\n'
                'slave_platform = "windows"\n',
                encoding="utf-8",
            )
            config = load_config(config_path, {})
            self.assertEqual(config.default_titella, "omarchy")
            self.assertEqual(set(config.titellas), {"omarchy", "winlab"})
            omarchy = config.titellas["omarchy"]
            winlab = config.titellas["winlab"]
            self.assertEqual(omarchy.serial_baud, 2000000)
            self.assertEqual(omarchy.slave_platform, "linux")
            self.assertEqual(omarchy.state_dir, root / "state" / "titellas" / "omarchy")
            self.assertEqual(winlab.transport, "wifi")
            self.assertEqual(winlab.wifi_host, "10.0.0.21")
            self.assertEqual(winlab.state_dir, root / "state" / "titellas" / "winlab")

    def test_rejects_unknown_default_titella_or_invalid_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad_default = root / "bad_default.toml"
            bad_default.write_text(
                'default_titella = "missing"\n[titella.omarchy]\nserial_port = "/dev/cu.fake"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "default_titella"):
                load_config(bad_default, {})
            bad_id = root / "bad_id.toml"
            bad_id.write_text(
                '[titella."Bad Name!"]\nserial_port = "/dev/cu.fake"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid titella id"):
                load_config(bad_id, {})

    def test_audit_root_is_global_and_environment_overrides_all_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(
                f'audit_root = "{root / "configured"}"\n'
                '[titella.one]\nserial_port = "/dev/fake-one"\n'
                '[titella.two]\nserial_port = "/dev/fake-two"\n',
                encoding="utf-8",
            )
            expected = (root / "environment").resolve()
            config = load_config(path, {"TITELLA_AUDIT_ROOT": str(expected)})
            self.assertEqual(config.audit_root, expected)
            self.assertEqual({item.audit_root for item in config.titellas.values()}, {expected})
            self.assertFalse(expected.exists())

    def test_audit_root_rejects_a_symlink_instead_of_authorizing_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "other"
            target.mkdir()
            link = root / "alias"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                load_config(root / "missing.toml", {"TITELLA_AUDIT_ROOT": str(link)})
            from unittest.mock import patch
            with patch("titella.config.DEFAULT_AUDIT_ROOT", link):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    load_config(root / "missing.toml", {})


if __name__ == "__main__":
    unittest.main()
