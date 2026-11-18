"""Regression: long ASCII text must be chunked, not sent as one frame.

The stage-0 HID microloader reads one line with ``[Console]::ReadLine()``.
When ``type_text`` sent a 2,980-character base64 payload as a single
``kb_text`` frame, the guest delivered only the first fragment (measured 254
characters) and the loader died with "Invalid length for a Base-64 char array
or string". The raw path already chunked at ``TEXT_CHARS_PER_FRAME``; the
ASCII fast path did not.
"""
from __future__ import annotations

import unittest

from titella.hid import TEXT_CHARS_PER_FRAME, HidController


class _Lease:
    def require(self, owner: str) -> None:
        return None


class _Link:
    """Records every HID request and answers ok."""

    transport = "serial"

    def __init__(self) -> None:
        self.port = "/dev/null"
        self.calls: list[tuple[str, dict]] = []

    def request(self, channel: str, op: str, body: dict, timeout: float = 10) -> dict:
        self.calls.append((op, dict(body)))
        return {"body": {"ok": True}}


def _controller(layout: str = "es_ES") -> tuple[HidController, _Link]:
    link = _Link()
    return HidController(link, _Lease(), screen=None, layout=layout), link


B64 = ("H4sIAAAAAAAC/7VY61PbSBL/7r+iN6jW0mGr/MKQc0wtsXndQkhhp6g6F5WSpTGejTyjmkBTor"
       "/fbtHD2RjSG5v9wNlpOn349c92oIxNyyOA/g0et+UIl6CNsEta7YglkHEVAPMMmERBBqkYHD7lSfb0"
       "0Czfg9iLtg74AaSNI51bQvMnMHWr1sQSmECPFTgZkJgG0559H5p2MgoFiwgFdx4MFNyYZmOWWBSxaD"
       "10G6jHC2CRM+laaCkRcJjpi")


class TypeTextChunkingTests(unittest.TestCase):
    def test_pure_ascii_text_is_split_into_bounded_frames(self) -> None:
        payload = B64 * 8  # 1,200 characters, still pure ASCII the map knows
        controller, link = _controller()
        result = controller.type_text("owner", payload, delay_ms=8)
        self.assertTrue(result.get("ok"), result)
        text_calls = [body for op, body in link.calls if op == "kb_text"]
        self.assertGreater(len(text_calls), 1, "the fast path must chunk")
        for body in text_calls:
            self.assertLessEqual(len(body["text"]), TEXT_CHARS_PER_FRAME)
        self.assertEqual("".join(body["text"] for body in text_calls), payload)
        self.assertEqual(result.get("chunks"), len(text_calls))

    def test_enter_rides_only_the_last_chunk(self) -> None:
        controller, link = _controller()
        controller.type_text("owner", "A" * (TEXT_CHARS_PER_FRAME * 2), delay_ms=8, enter=True)
        text_calls = [body for op, body in link.calls if op == "kb_text"]
        self.assertTrue(all(body["enter"] is False for body in text_calls[:-1]))
        self.assertTrue(text_calls[-1]["enter"])

    def test_a_short_string_stays_one_frame(self) -> None:
        controller, link = _controller()
        result = controller.type_text("owner", "powershell", delay_ms=8)
        text_calls = [body for op, body in link.calls if op == "kb_text"]
        self.assertEqual(len(text_calls), 1)
        self.assertEqual(text_calls[0]["text"], "powershell")
        self.assertNotIn("chunks", result)

    def test_untypeable_characters_still_fail_before_any_frame(self) -> None:
        # ``|`` has no es_ES mapping in this keymap, so the request is refused
        # before any frame is emitted.
        controller, link = _controller("es_ES")
        result = controller.type_text("owner", "a\u00acb", delay_ms=8)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["error"]["code"], "untypeable_characters")
        self.assertEqual(link.calls, [])


if __name__ == "__main__":
    unittest.main()
