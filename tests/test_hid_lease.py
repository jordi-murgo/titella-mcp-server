from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from titella.config import load_config
from titella.hid import HidLease
from titella.serial_link import TransportError


class HidLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lease = HidLease(load_config(environ={"TITELLA_STATE_DIR": self.temp.name}))

    def test_excludes_other_owner_and_release_is_idempotent_only_for_owner(self) -> None:
        self.assertTrue(self.lease.acquire("alice", 30)["ok"])
        denied = self.lease.acquire("bob", 30)
        self.assertEqual(denied["status"], "error")
        self.assertEqual(denied["error"]["code"], "hid_lease_busy")
        self.assertTrue(denied["error"]["retryable"])
        self.lease.require("alice")
        with self.assertRaises(TransportError):
            self.lease.require("bob")
        self.assertTrue(self.lease.release("alice")["ok"])
        released_again = self.lease.release("alice")
        self.assertFalse(released_again["ok"])
        self.assertEqual(released_again["error"]["code"], "hid_lease_not_owned")
        self.assertFalse(released_again["error"]["retryable"])

    def test_state_file_is_private(self) -> None:
        self.lease.acquire("alice", 30)
        path = Path(self.temp.name) / "hid-lease.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
