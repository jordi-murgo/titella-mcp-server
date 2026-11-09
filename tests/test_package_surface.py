from __future__ import annotations

import asyncio
import tempfile
import unittest

from titella.config import load_config
from titella.mcp_server import create_server
from titella.probe import simulate
from titella.screen import PNG_SIGNATURE, png_resolution


class PackageSurfaceTests(unittest.TestCase):
    def test_existing_mcp_tool_names_remain_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server = create_server(load_config(environ={"TITELLA_STATE_DIR": temp}))
            names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertTrue({
            "titella_list", "titella_discover", "titella_upsert", "titella_remove", "titella_set_default",
            "screen", "lock_acquire", "lock_renew", "lock_release", "lock_status",
            "mouse_abs", "mouse_rel", "click", "wheel", "key", "type", "kvm_status",
        }.issubset(names))

    def test_png_resolution_reads_ihdr(self) -> None:
        png = PNG_SIGNATURE + b"\x00\x00\x00\x0dIHDR" + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big")
        self.assertEqual(png_resolution(png), (1920, 1080))

    def test_simulated_probe_exercises_fragmented_frame(self) -> None:
        self.assertEqual(simulate()["frames"], 1)
        self.assertTrue(simulate()["ok"])


if __name__ == "__main__":
    unittest.main()
