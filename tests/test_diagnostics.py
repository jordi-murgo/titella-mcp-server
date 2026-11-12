"""Regression tests for the aggregated KVM diagnostics."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from titella.config import load_config
from titella.diagnostics import diagnose, next_steps

FIRMWARE_OK = {"reachable": True, "firmware": "1.3.0", "protocol": 1}
LEASE_FREE = {"ok": True, "locked": False, "owner": None, "expires_in": 0}
LEASE_HELD = {"ok": True, "locked": True, "owner": "ops", "expires_in": 600, "you": True}
AGENT_OFF = {"status": "not_started"}
AGENT_READY = {"status": "ready"}


class DiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = load_config(environ={
            # A pinned absent config keeps the developer's own TOML (which may
            # set a platform) out of the assertions.
            "TITELLA_CONFIG": str(Path(self.temp.name) / "absent.toml"),
            "TITELLA_STATE_DIR": self.temp.name,
        })

    def _steps(self, **overrides):
        values = {
            "firmware": FIRMWARE_OK,
            "lease": LEASE_HELD,
            "agent": AGENT_READY,
            "resolution": (2560, 1440),
            "capture_age_s": 1.0,
        }
        values.update(overrides)
        return next_steps(self.config, **values)

    def test_ready_stack_has_no_pending_action(self) -> None:
        self.assertEqual(
            self._steps(), ["Everything is ready: lease, agent and capture are current."]
        )

    def test_unreachable_firmware_is_reported_first(self) -> None:
        steps = self._steps(firmware={"reachable": False, "error": {"code": "request_timeout"}})
        self.assertIn("TITELLA_TRANSPORT", steps[0])
        self.assertIn("envelope v1", steps[0])

    def test_free_lease_blocks_hid_operations(self) -> None:
        steps = self._steps(lease=LEASE_FREE)
        self.assertTrue(any("lock_acquire" in step for step in steps))

    def test_missing_agent_explains_bootstrap_failure_causes(self) -> None:
        steps = self._steps(agent=AGENT_OFF)
        bootstrap = next(step for step in steps if "slave_bootstrap" in step)
        # No platform selected: the actionable advice is the explicit choice,
        # never Linux-only group advice.
        self.assertIn("platform", bootstrap)
        self.assertNotIn("uucp", bootstrap)

    def test_agent_advice_follows_the_selected_platform(self) -> None:
        linux = self._steps(agent={"status": "not_started", "platform": "linux"})
        self.assertIn("uucp", next(step for step in linux if "slave_bootstrap" in step))
        windows = self._steps(agent={"status": "not_started", "platform": "windows"})
        step = next(step for step in windows if "slave_bootstrap" in step)
        self.assertNotIn("uucp", step)
        self.assertIn("slave_cdc_port", step)
        macos = self._steps(agent={"status": "not_started", "platform": "macos"})
        step = next(step for step in macos if "slave_bootstrap" in step)
        self.assertNotIn("uucp", step)
        # The configured platform also drives the advice before any bootstrap.
        self.config = load_config(environ={
            "TITELLA_CONFIG": str(Path(self.temp.name) / "absent.toml"),
            "TITELLA_STATE_DIR": self.temp.name,
            "TITELLA_SLAVE_PLATFORM": "windows",
        })
        configured = self._steps(agent=AGENT_OFF)
        self.assertIn("slave_cdc_port", next(step for step in configured if "slave_bootstrap" in step))

    def test_missing_agent_step_requires_a_verified_layout(self) -> None:
        for platform in (None, "windows", "linux", "macos"):
            with self.subTest(platform=platform):
                agent = dict(AGENT_OFF)
                if platform is not None:
                    agent["platform"] = platform
                step = next(step for step in self._steps(agent=agent)
                            if "slave_bootstrap" in step)
                self.assertIn("layout_confirmed", step)
                self.assertIn("layout=", step)
                self.assertIn("slave_layout_required", step)

    def test_no_capture_requires_screen_before_absolute_mouse(self) -> None:
        steps = self._steps(resolution=None, capture_age_s=None)
        self.assertTrue(any("screen()" in step and "screen_required" in step for step in steps))

    def test_stale_capture_is_flagged_but_not_fatal(self) -> None:
        steps = self._steps(capture_age_s=600.0)
        self.assertEqual(len(steps), 1)
        self.assertIn("screen()", steps[0])

    def test_hid_failure_never_gives_com_advice(self) -> None:
        self.config = load_config(environ={
            "TITELLA_CONFIG": str(Path(self.temp.name) / "absent.toml"),
            "TITELLA_STATE_DIR": self.temp.name,
            "TITELLA_SLAVE_PLATFORM": "windows",
            "TITELLA_SLAVE_TRANSPORT": "hid",
        })
        step = next(step for step in self._steps(agent=AGENT_OFF)
                    if "slave_bootstrap" in step)
        self.assertIn("slave_transport_unsupported", step)
        self.assertIn("skip_launch=true", step)
        # A HID failure is never a COM problem: the advice must not send the
        # operator to pick a COM port (the trailing sentence only states that
        # no COM port participates).
        self.assertNotIn("slave_cdc_port", step)
        self.assertNotIn("COM5", step)
        self.assertNotIn("COM[1-9]", step)

    def test_diagnose_reports_the_selected_slave_transport(self) -> None:
        report = diagnose(
            self.config,
            firmware=FIRMWARE_OK,
            lease=LEASE_HELD,
            agent=AGENT_READY,
            resolution=(2560, 1440),
            capture_age_s=0.5,
        )
        self.assertEqual(report["slave_transport"], "cdc")

    def test_diagnose_reports_capture_configuration(self) -> None:
        report = diagnose(
            self.config,
            firmware=FIRMWARE_OK,
            lease=LEASE_HELD,
            agent=AGENT_READY,
            resolution=(2560, 1440),
            capture_age_s=0.5,
        )
        self.assertEqual(report["capture"]["source"], "USB3 Video")
        self.assertEqual(report["capture"]["pixel_format"], "uyvy422")
        self.assertEqual(report["capture"]["resolution"], [2560, 1440])
        self.assertEqual(report["capture"]["last_frame_age_s"], 0.5)


if __name__ == "__main__":
    unittest.main()
