from __future__ import annotations

import unittest

from titella.shell_client import ByteRing


class ByteRingTests(unittest.TestCase):
    def test_absolute_offsets_report_dropped_history(self) -> None:
        ring = ByteRing(limit=8)
        ring.append(b"abcd")
        ring.append(b"efgh")
        ring.append(b"ijkl")
        data, dropped, next_offset = ring.read(0, 32)
        self.assertEqual(data, b"efghijkl")
        self.assertEqual(dropped, 4)
        self.assertEqual(next_offset, 12)
        data, dropped, next_offset = ring.read(8, 2)
        self.assertEqual((data, dropped, next_offset), (b"ij", None, 10))

    def test_partial_eviction_keeps_exact_absolute_positions(self) -> None:
        ring = ByteRing(limit=5)
        ring.append(b"01234567")
        data, dropped, next_offset = ring.read(3, 5)
        self.assertEqual(data, b"34567")
        self.assertIsNone(dropped)
        self.assertEqual(next_offset, 8)


if __name__ == "__main__":
    unittest.main()
