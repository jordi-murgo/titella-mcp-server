"""Channel capability discovery and slave-variant selection."""
from __future__ import annotations

import json
import unittest

from titella.capabilities import (
    CAPS_MARKER,
    PROBE_DELIVERY,
    PROBE_SCRIPT,
    apply_transport_facts,
    parse_caps,
    recommend_variant,
)

LENOVO_OUTPUT = CAPS_MARKER + json.dumps({
    "cameras": ["Integrated Camera", "USB2.0 Camera"],
    "audio_endpoints": ["Remote Audio"],
    "media": ["Realtek High Definition Audio", "Poly Studio X30 Speakers"],
    "clipboard_text": True,
    "clipboard_image": True,
})


class ParseCapsTest(unittest.TestCase):
    def test_reads_devices_and_local_clipboard_api(self) -> None:
        caps = parse_caps("noise before\n" + LENOVO_OUTPUT + "\nnoise after")
        self.assertEqual(
            caps["video_in"]["devices"], ["Integrated Camera", "USB2.0 Camera"]
        )
        self.assertTrue(caps["video_in"]["available"])
        self.assertEqual(caps["audio_in"]["devices"], ["Remote Audio"])
        self.assertTrue(caps["audio_in"]["available"])
        self.assertTrue(caps["clipboard_text"]["local_api"])
        self.assertTrue(caps["clipboard_image"]["local_api"])

    def test_transport_directions_start_unknown(self) -> None:
        caps = parse_caps(LENOVO_OUTPUT)
        self.assertIsNone(caps["clipboard_text"]["m2s"])
        self.assertIsNone(caps["clipboard_text"]["s2m"])

    def test_single_device_is_normalised_to_a_list(self) -> None:
        output = CAPS_MARKER + json.dumps({
            "cameras": "Integrated Camera",
            "audio_endpoints": "Remote Audio",
            "media": "",
            "clipboard_text": True,
            "clipboard_image": False,
        })
        caps = parse_caps(output)
        self.assertEqual(caps["video_in"]["devices"], ["Integrated Camera"])
        self.assertEqual(caps["audio_in"]["devices"], ["Remote Audio"])

    def test_no_camera_reports_unavailable(self) -> None:
        output = CAPS_MARKER + json.dumps({
            "cameras": [],
            "audio_endpoints": [],
            "media": [],
            "clipboard_text": True,
            "clipboard_image": False,
        })
        caps = parse_caps(output)
        self.assertFalse(caps["video_in"]["available"])
        self.assertFalse(caps["audio_in"]["available"])

    def test_rejects_output_without_the_marker(self) -> None:
        with self.assertRaises(ValueError):
            parse_caps("Get-PnpDevice: no devices found")

    def test_rejects_malformed_payload(self) -> None:
        with self.assertRaises(ValueError):
            parse_caps(CAPS_MARKER + "{not json")

    def test_probe_script_avoids_commas(self) -> None:
        # A comma is the character most often mangled by a non-US layout when
        # the script is typed instead of sent over an agent channel.
        self.assertNotIn(",", PROBE_SCRIPT)

    def test_probe_script_prints_the_marker(self) -> None:
        self.assertIn(CAPS_MARKER, PROBE_SCRIPT)

    def test_probe_is_delivered_over_the_clipboard(self) -> None:
        # X11 clients mangle `[` and `]`, which the probe needs for its type
        # accelerators; it must therefore travel as clipboard text.
        self.assertIn("[", PROBE_SCRIPT)
        self.assertEqual(PROBE_DELIVERY, "clipboard")


class RecommendVariantTest(unittest.TestCase):
    def _caps(self, **directions):
        base = parse_caps(LENOVO_OUTPUT)
        return apply_transport_facts(base, **directions)

    def test_bidirectional_text_selects_clipboard(self) -> None:
        choice = recommend_variant(
            self._caps(text_m2s=True, text_s2m=True, image_m2s=False, image_s2m=False)
        )
        self.assertEqual(choice["agent_protocol"], "clipboard")

    def test_missing_image_direction_costs_throughput_only(self) -> None:
        choice = recommend_variant(
            self._caps(text_m2s=True, text_s2m=True, image_m2s=False, image_s2m=False)
        )
        joined = " ".join(choice["reasons"])
        self.assertIn("text chunks", joined)
        self.assertEqual(choice["agent_protocol"], "clipboard")

    def test_image_direction_is_reported_when_available(self) -> None:
        choice = recommend_variant(
            self._caps(text_m2s=True, text_s2m=True, image_m2s=True, image_s2m=False)
        )
        self.assertIn("bulk transfer is fast", " ".join(choice["reasons"]))

    def test_one_way_text_cannot_carry_the_protocol(self) -> None:
        for m2s, s2m in ((True, False), (False, True)):
            choice = recommend_variant(
                self._caps(text_m2s=m2s, text_s2m=s2m, image_m2s=True, image_s2m=False)
            )
            self.assertEqual(choice["agent_protocol"], "none")

    def test_no_clipboard_falls_back_to_visual(self) -> None:
        choice = recommend_variant(
            self._caps(text_m2s=False, text_s2m=False, image_m2s=False, image_s2m=False)
        )
        self.assertEqual(choice["agent_protocol"], "visual")

    def test_unknown_directions_are_not_treated_as_working(self) -> None:
        choice = recommend_variant(parse_caps(LENOVO_OUTPUT))
        self.assertEqual(choice["agent_protocol"], "visual")

    def test_free_rdp_measurement_selects_clipboard(self) -> None:
        # Measured live on the FreeRDP window: text works both ways with no
        # activation, so the bench runs the clipboard variant.
        choice = recommend_variant(
            self._caps(text_m2s=True, text_s2m=True, image_m2s=None, image_s2m=None)
        )
        self.assertEqual(choice["agent_protocol"], "clipboard")


if __name__ == "__main__":
    unittest.main()
