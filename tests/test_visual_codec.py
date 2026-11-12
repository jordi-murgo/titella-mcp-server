"""Behavioral tests for the local eight-color visual data codec."""
from __future__ import annotations

import json
import os
import random
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from titella.visual_codec import decode_frame, encode_frame

from titella import png_codec
from titella import visual_codec as vc
from titella.visual_codec import (
    AmbiguousFrameError,
    FrameEncodingError,
    FrameNotFoundError,
    InvalidFrameError,
    VisualCodecError,
    max_payload_bytes,
)

# Independent copy of the palette documented in the module spec; the decoder
# samples the received frame, so tests must not depend on the module constant.
TEST_PALETTE = [
    (220, 40, 40),    # 0
    (30, 160, 50),    # 1
    (20, 40, 235),    # 2
    (30, 210, 200),   # 3
    (220, 30, 210),   # 4
    (210, 200, 30),   # 5
    (230, 120, 20),   # 6
    (120, 30, 235),   # 7
]


def _sample_bytes(count: int) -> bytes:
    return bytes((index * 37 + 11) & 0xFF for index in range(count))


def _fill(rgb: bytearray, width: int, x0: int, y0: int, x1: int, y1: int, color) -> None:
    height = len(rgb) // (width * 3)
    x0, x1 = max(0, x0), min(width, x1)
    y0, y1 = max(0, y0), min(height, y1)
    if x0 >= x1 or y0 >= y1:
        return
    row = bytes(color) * (x1 - x0)
    for y in range(y0, y1):
        base = (y * width + x0) * 3
        rgb[base:base + (x1 - x0) * 3] = row


def _desktop(width: int = 1920, height: int = 1080) -> bytearray:
    rgb = bytearray(width * height * 3)
    for y in range(height):
        base = y * width * 3
        shade = 22 + (y * 6) // max(1, height)
        rgb[base:base + width * 3] = bytes((shade, shade + 2, shade + 8)) * width
    # Distractors: a bright block, high-contrast stripes (pitch-8 bait), a
    # colored rectangle and a gray bar. None may overlap a tested placement.
    _fill(rgb, width, 900, 100, 1060, 160, (255, 255, 255))
    for x in range(900, 1060, 16):
        _fill(rgb, width, x, 200, x + 8, 260, (0, 0, 0))
        _fill(rgb, width, x + 8, 200, x + 16, 260, (255, 255, 255))
    _fill(rgb, width, 1200, 500, 1700, 900, (40, 60, 140))
    _fill(rgb, width, 60, 900, 500, 960, (90, 90, 96))
    return rgb


def _paste(canvas: bytearray, width: int, frame_rgb: bytes, frame_w: int, frame_h: int, ox: int, oy: int) -> None:
    for y in range(frame_h):
        source = frame_rgb[y * frame_w * 3:(y + 1) * frame_w * 3]
        destination = ((oy + y) * width + ox) * 3
        canvas[destination:destination + frame_w * 3] = source


def _encode_png(rgb, width: int, height: int) -> bytes:
    return png_codec.encode_png_rgb(width, height, bytes(rgb))


def _decode_png(png: bytes):
    return png_codec.decode_png(png)


def _place(png: bytes, ox: int, oy: int, size=(1920, 1080), canvas=None):
    width, height = size
    if canvas is None:
        canvas = _desktop(width, height)
    frame_w, frame_h, frame_rgb = _decode_png(png)
    _paste(canvas, width, frame_rgb, frame_w, frame_h, ox, oy)
    return width, height, canvas


def _scale_nearest(rgb: bytes, width: int, height: int, factor: int):
    out_w, out_h = width * factor, height * factor
    out = bytearray(out_w * out_h * 3)
    for y in range(height):
        source = rgb[y * width * 3:(y + 1) * width * 3]
        row = b"".join(source[x * 3:x * 3 + 3] * factor for x in range(width))
        for k in range(factor):
            base = (y * factor + k) * out_w * 3
            out[base:base + out_w * 3] = row
    return bytes(out), out_w, out_h


def _scale_fractional(rgb: bytes, width: int, height: int, numerator: int, denominator: int):
    out_w = (width * numerator) // denominator
    out_h = (height * numerator) // denominator
    out = bytearray(out_w * out_h * 3)
    for y in range(out_h):
        sy = (y * denominator) // numerator
        source = rgb[sy * width * 3:(sy + 1) * width * 3]
        row = bytearray(out_w * 3)
        for x in range(out_w):
            sx = (x * denominator) // numerator
            row[x * 3:x * 3 + 3] = source[sx * 3:sx * 3 + 3]
        out[y * out_w * 3:(y + 1) * out_w * 3] = row
    return bytes(out), out_w, out_h


def _channel_drift(rgb: bytes, gains, offsets, noise: float, seed: int) -> bytes:
    rng = random.Random(seed)
    out = bytearray(len(rgb))
    for index in range(len(rgb)):
        channel = index % 3
        value = rgb[index] * gains[channel] + offsets[channel]
        value += rng.uniform(-noise, noise)
        out[index] = 0 if value < 0 else (255 if value > 255 else int(value))
    return bytes(out)


def _cell_origin(t: int, columns: int, cell_size: int):
    """Grid origin in pixels of global cell index t, per the module spec."""
    if t < 200:
        grid_x, grid_y = 2 + (t % 16), 4 + (t // 16)
    else:
        q = t - 200
        grid_x, grid_y = 2 + (q % columns), 17 + (q // columns)
    return grid_x * cell_size, grid_y * cell_size


def _cell_index_at(png: bytes, t: int, columns: int, cell_size: int) -> int:
    width, _height, rgb = _decode_png(png)
    x, y = _cell_origin(t, columns, cell_size)
    index = ((y + cell_size // 2) * width + x + cell_size // 2) * 3
    color = (rgb[index], rgb[index + 1], rgb[index + 2])
    return vc.PALETTE.index(color)


def _paint_cell(png: bytes, t: int, color_index: int, columns: int, cell_size: int) -> bytes:
    width, height, rgb = _decode_png(png)
    x, y = _cell_origin(t, columns, cell_size)
    block = bytes(vc.PALETTE[color_index]) * cell_size
    buf = bytearray(rgb)
    for row in range(y, y + cell_size):
        base = (row * width + x) * 3
        buf[base:base + cell_size * 3] = block
    return _encode_png(buf, width, height)


def _flip_cell_color(png: bytes, t: int, columns: int, cell_size: int) -> bytes:
    """Substitutes one cell with the plane-complement color (all bits differ)."""
    original = _cell_index_at(png, t, columns, cell_size)
    return _paint_cell(png, t, original ^ 0x7, columns, cell_size)


def _fixture_frame(
    payload: bytes,
    *,
    session_id: int,
    sequence: int = 0,
    fragment_index: int = 0,
    fragment_count: int = 1,
    columns: int = 20,
    cell_size: int = 6,
    version: int = vc.FORMAT_VERSION,
    palette=None,
    corrupt_packet_crc: bool = False,
) -> bytes:
    """Independent encoder written from the documented wire spec only."""
    palette = TEST_PALETTE if palette is None else palette
    total_bits = (37 + len(payload)) * 8
    codewords = -(-total_bits // 12)
    payload_cells = (codewords - 25) * 8
    data_rows = -(-payload_cells // columns) if payload_cells else 0
    header = struct.pack(
        ">4sBBHIIHHHHHBBB",
        b"TVF1", version, 0, len(payload), session_id, sequence,
        fragment_index, fragment_count, columns, data_rows, cell_size, 2, 0, 0,
    )
    header_crc = zlib.crc32(header) & 0xFFFFFFFF
    packet_crc = zlib.crc32(header + payload) & 0xFFFFFFFF
    if corrupt_packet_crc:
        packet_crc ^= 0xFFFFFFFF
    stream = header + struct.pack(">II", header_crc, packet_crc) + payload

    def plane_byte(nibble: int) -> int:
        d0, d1 = nibble & 1, (nibble >> 1) & 1
        d2, d3 = (nibble >> 2) & 1, (nibble >> 3) & 1
        x1 = d0 ^ d1 ^ d3
        x2 = d0 ^ d2 ^ d3
        x4 = d1 ^ d2 ^ d3
        x8 = x1 ^ x2 ^ d0 ^ x4 ^ d1 ^ d2 ^ d3
        return (
            (x1 << 7) | (x2 << 6) | (d0 << 5) | (x4 << 4)
            | (d1 << 3) | (d2 << 2) | (d3 << 1) | x8
        )

    stream_bits = len(stream) * 8
    colors: list[int] = []
    for codeword in range(codewords):
        words = []
        for plane in range(3):
            nibble = 0
            for k in range(4):
                index = codeword * 12 + plane * 4 + k
                if index < stream_bits:
                    nibble = (nibble << 1) | ((stream[index >> 3] >> (7 - index & 7)) & 1)
                else:
                    nibble <<= 1
            words.append(plane_byte(nibble))
        for k in range(8):
            shift = 7 - k
            colors.append(
                (((words[0] >> shift) & 1) << 2)
                | (((words[1] >> shift) & 1) << 1)
                | ((words[2] >> shift) & 1)
            )

    grid_cols = columns + 4
    grid_rows = data_rows + 19
    blocks = [bytes(color) * cell_size for color in palette]
    black = bytes((0, 0, 0)) * cell_size
    white = bytes((255, 255, 255)) * cell_size
    buf = bytearray()
    for grid_y in range(grid_rows):
        row = [black] * grid_cols
        if grid_y == 2:
            for j in range(columns):
                row[2 + j] = black if j % 2 == 0 else white
        elif grid_y == 3:
            row[2] = black
            row[3] = white
            for i in range(8):
                row[4 + i] = blocks[i]
        elif 4 <= grid_y < 17:
            for c in range(min(16, columns)):
                t = (grid_y - 4) * 16 + c
                if t < 200:
                    row[2 + c] = blocks[colors[t]]
        elif grid_y >= 17:
            for c in range(columns):
                t = 200 + (grid_y - 17) * columns + c
                if t < len(colors):
                    row[2 + c] = blocks[colors[t]]
        buf += b"".join(row) * cell_size
    return _encode_png(buf, grid_cols * cell_size, grid_rows * cell_size)


class VisualCodecTests(unittest.TestCase):
    def test_binary_payload_and_fragment_identity_round_trip(self):
        payload = bytes(range(256)) + b"\x00\xffvisual-frame"
        image = encode_frame(
            payload, session_id=0x12345678, sequence=19,
            fragment_index=2, fragment_count=5,
        )
        frame = decode_frame(image)
        self.assertEqual(frame.payload, payload)
        self.assertEqual(frame.session_id, 0x12345678)
        self.assertEqual(frame.sequence, 19)
        self.assertEqual(frame.fragment_index, 2)
        self.assertEqual(frame.fragment_count, 5)

    def test_empty_payload_round_trip(self):
        image = encode_frame(b"", session_id=0)
        frame = decode_frame(image)
        self.assertEqual(frame.payload, b"")
        self.assertEqual(frame.session_id, 0)
        self.assertEqual(frame.sequence, 0)
        self.assertEqual(frame.fragment_index, 0)
        self.assertEqual(frame.fragment_count, 1)
        self.assertEqual(frame.columns, 48)
        self.assertEqual(frame.cell_size, 8)
        self.assertEqual(frame.data_rows, 0)
        self.assertEqual(frame.scale, 1)
        self.assertEqual(frame.corrected_cells, 0)
        self.assertEqual(frame.bbox, (0, 0, 416, 152))
        with self.assertRaises(AttributeError):
            frame.payload = b"mutated"

    def test_defaults_and_metadata_round_trip(self):
        payload = _sample_bytes(64)
        frame = decode_frame(encode_frame(payload, session_id=7))
        self.assertEqual(frame.payload, payload)
        self.assertEqual(frame.session_id, 7)
        self.assertEqual((frame.sequence, frame.fragment_index, frame.fragment_count), (0, 0, 1))
        self.assertEqual((frame.columns, frame.cell_size, frame.data_rows), (48, 8, 8))
        self.assertEqual(frame.codeword_count, 68)
        self.assertEqual(frame.bbox, (0, 0, 416, 216))


class EncodeValidationTests(unittest.TestCase):
    def test_argument_validation(self):
        payload = b"x"
        for kwargs in (
            {"session_id": -1},
            {"session_id": 2 ** 32},
            {"session_id": 1, "sequence": -1},
            {"session_id": 1, "fragment_count": 0},
            {"session_id": 1, "fragment_count": 2, "fragment_index": 2},
            {"session_id": 1, "columns": 15},
            {"session_id": 1, "columns": 257},
            {"session_id": 1, "cell_size": 3},
            {"session_id": 1, "cell_size": 33},
        ):
            with self.assertRaises(FrameEncodingError):
                encode_frame(payload, **kwargs)
        with self.assertRaises(FrameEncodingError):
            encode_frame("text", session_id=1)
        with self.assertRaises(FrameEncodingError):
            encode_frame(b"x" * 65536, session_id=1)
        # Bytes-like inputs are accepted and preserved.
        image = encode_frame(bytearray(b"\x00\x01"), session_id=1)
        self.assertEqual(decode_frame(image).payload, b"\x00\x01")

    def test_capacity_boundary(self):
        rows_cap = min(vc.MAX_DATA_ROWS, vc.MAX_FRAME_SIDE_PX // 4 - 19)
        expected = (((rows_cap * 48) // 8 + 25) * 12 - 296) // 8
        self.assertEqual(max_payload_bytes(48, 4), expected)
        self.assertEqual(max_payload_bytes(48, 4), 9216)
        boundary = _sample_bytes(max_payload_bytes(48, 4))
        frame = decode_frame(encode_frame(boundary, session_id=3, columns=48, cell_size=4))
        self.assertEqual(frame.payload, boundary)
        self.assertEqual(frame.bbox, (0, 0, 208, 4172))
        with self.assertRaises(FrameEncodingError):
            encode_frame(boundary + b"\x00", session_id=3, columns=48, cell_size=4)


class ScreenshotTests(unittest.TestCase):
    def test_placement_on_dark_desktop_with_distractors(self):
        payload = _sample_bytes(256)
        image = encode_frame(
            payload, session_id=0x12345678, sequence=19,
            fragment_index=2, fragment_count=5,
        )
        width, height, canvas = _place(image, 213, 117)
        frame = decode_frame(_encode_png(canvas, width, height))
        self.assertEqual(frame.payload, payload)
        self.assertEqual(frame.session_id, 0x12345678)
        self.assertEqual(frame.sequence, 19)
        self.assertEqual((frame.fragment_index, frame.fragment_count), (2, 5))
        self.assertEqual(frame.bbox, (213, 117, 416, 384))
        self.assertEqual(frame.scale, 1)
        self.assertEqual(frame.corrected_cells, 0)

    def test_integer_scaling(self):
        payload = _sample_bytes(64)
        image = encode_frame(payload, session_id=11)
        for factor, placement in ((2, (213, 620)), (3, (60, 60))):
            _w, _h, frame_rgb = _decode_png(image)
            scaled, scaled_w, scaled_h = _scale_nearest(frame_rgb, 416, 216, factor)
            width, height, canvas = _place(
                _encode_png(scaled, scaled_w, scaled_h), *placement
            )
            frame = decode_frame(_encode_png(canvas, width, height))
            self.assertEqual(frame.payload, payload)
            self.assertEqual(frame.scale, factor)
            self.assertEqual(
                frame.bbox,
                (placement[0], placement[1], 416 * factor, 216 * factor),
            )

    def test_fractional_scaling_rejected(self):
        payload = _sample_bytes(64)
        image = encode_frame(payload, session_id=12)
        _w, _h, frame_rgb = _decode_png(image)
        scaled, scaled_w, scaled_h = _scale_fractional(frame_rgb, 416, 216, 4, 3)
        width, height, canvas = _place(
            _encode_png(scaled, scaled_w, scaled_h), 100, 100
        )
        with self.assertRaises(VisualCodecError):
            decode_frame(_encode_png(canvas, width, height))

    def test_channel_drift_and_noise(self):
        payload = _sample_bytes(96)
        image = encode_frame(payload, session_id=13)
        width, height, canvas = _place(image, 100, 120, size=(900, 600))
        drifted = _channel_drift(
            bytes(canvas), (0.85, 1.15, 0.90), (-10, 12, 4), 10.0, 1234
        )
        frame = decode_frame(_encode_png(drifted, width, height))
        self.assertEqual(frame.payload, payload)
        self.assertEqual(frame.bbox, (100, 120, 416, 240))

    def test_variable_geometry(self):
        for columns, cell, payload_len, placement in (
            (16, 4, 40, (17, 23)),
            (120, 10, 500, (100, 400)),
        ):
            payload = _sample_bytes(payload_len)
            image = encode_frame(
                payload, session_id=21, columns=columns, cell_size=cell
            )
            width, height, canvas = _place(image, *placement)
            frame = decode_frame(_encode_png(canvas, width, height))
            self.assertEqual(frame.payload, payload)
            self.assertEqual(frame.columns, columns)
            self.assertEqual(frame.cell_size, cell)
            self.assertEqual(
                frame.bbox,
                (
                    placement[0], placement[1],
                    (columns + 4) * cell,
                    (frame.data_rows + 19) * cell,
                ),
            )


class DamageTests(unittest.TestCase):
    def test_single_cell_damage_corrected(self):
        payload = _sample_bytes(64)
        image = encode_frame(payload, session_id=31)
        for t in (5, 300):
            damaged = _flip_cell_color(image, t, 48, 8)
            frame = decode_frame(damaged)
            self.assertEqual(frame.payload, payload)
            self.assertEqual(frame.corrected_cells, 1)

    def test_double_cell_damage_rejected(self):
        payload = _sample_bytes(64)
        image = encode_frame(payload, session_id=32)
        # Two damaged cells in the header strip codeword 0: the header can no
        # longer be confirmed, so the image is rejected as frame-not-found.
        header_damage = _flip_cell_color(image, 5, 48, 8)
        header_damage = _flip_cell_color(header_damage, 6, 48, 8)
        with self.assertRaises(VisualCodecError):
            decode_frame(header_damage)
        # Two damaged cells in one payload codeword reach the confirmed-frame
        # path and fail ECC after the header was validated.
        payload_damage = _flip_cell_color(image, 300, 48, 8)
        payload_damage = _flip_cell_color(payload_damage, 301, 48, 8)
        with self.assertRaises(InvalidFrameError) as raised:
            decode_frame(payload_damage)
        self.assertIn("payload_ecc_failure", str(raised.exception))

    def test_hamming_invariants(self):
        def equations(nibble: int) -> int:
            d0, d1 = nibble & 1, (nibble >> 1) & 1
            d2, d3 = (nibble >> 2) & 1, (nibble >> 3) & 1
            x1 = d0 ^ d1 ^ d3
            x2 = d0 ^ d2 ^ d3
            x4 = d1 ^ d2 ^ d3
            x8 = x1 ^ x2 ^ d0 ^ x4 ^ d1 ^ d2 ^ d3
            return (
                (x1 << 7) | (x2 << 6) | (d0 << 5) | (x4 << 4)
                | (d1 << 3) | (d2 << 2) | (d3 << 1) | x8
            )

        for nibble in range(16):
            word = vc._HAM_ENC[nibble]
            self.assertEqual(word, equations(nibble))
            self.assertEqual(vc._HAM_DEC[word], (nibble, False))
            for position in range(8):
                self.assertEqual(
                    vc._HAM_DEC[word ^ (1 << position)], (nibble, True)
                )
            for first in range(8):
                for second in range(first + 1, 8):
                    self.assertIsNone(
                        vc._HAM_DEC[word ^ (1 << first) ^ (1 << second)]
                    )
        decodable = sum(entry is not None for entry in vc._HAM_DEC)
        self.assertEqual(decodable, 16 * 9)


class FixtureTests(unittest.TestCase):
    def test_independent_fixture_round_trip(self):
        payload = b"independent fixture payload"
        image = _fixture_frame(payload, session_id=0xDEADBEEF, sequence=7,
                               fragment_index=3, fragment_count=9)
        frame = decode_frame(image)
        self.assertEqual(frame.payload, payload)
        self.assertEqual(frame.session_id, 0xDEADBEEF)
        self.assertEqual(frame.sequence, 7)
        self.assertEqual((frame.fragment_index, frame.fragment_count), (3, 9))
        # 27-byte payload -> 43 codewords -> 144 payload cells -> 8 rows of 20.
        self.assertEqual((frame.columns, frame.cell_size, frame.data_rows), (20, 6, 8))
        self.assertEqual(frame.bbox, (0, 0, 144, 162))
        self.assertEqual(frame.corrected_cells, 0)

    def test_unsupported_version_rejected(self):
        image = _fixture_frame(b"fixture", session_id=1, version=99)
        with self.assertRaises(InvalidFrameError) as raised:
            decode_frame(image)
        self.assertIn("unsupported_format_version", str(raised.exception))

    def test_packet_checksum_mismatch_rejected(self):
        image = _fixture_frame(b"fixture", session_id=1, corrupt_packet_crc=True)
        with self.assertRaises(InvalidFrameError) as raised:
            decode_frame(image)
        self.assertIn("checksum_mismatch", str(raised.exception))

    def test_degenerate_palette_rejected(self):
        palette = list(TEST_PALETTE)
        palette[3] = palette[2]  # two identical samples
        image = _fixture_frame(b"fixture", session_id=1, palette=palette)
        with self.assertRaises(FrameNotFoundError):
            decode_frame(image)


class RejectionTests(unittest.TestCase):
    def test_truncated_png_rejected(self):
        image = encode_frame(_sample_bytes(32), session_id=41)
        for broken in (image[: len(image) // 2], b"", b"not a png stream at all"):
            with self.assertRaises(VisualCodecError):
                decode_frame(broken)
        with self.assertRaises(VisualCodecError):
            decode_frame(123)

    def test_oversized_input_rejected(self):
        small = png_codec.encode_png_rgb(4, 4, bytes(48))
        patched = bytearray(small)
        struct.pack_into(">II", patched, 16, 5000, 5000)
        with self.assertRaises(InvalidFrameError) as raised:
            decode_frame(bytes(patched))
        self.assertIn("input_too_large", str(raised.exception))

    def test_unrelated_images_rejected(self):
        gradient = bytearray(320 * 240 * 3)
        for y in range(240):
            for x in range(320):
                index = (y * 320 + x) * 3
                gradient[index:index + 3] = bytes(((x * 5) & 0xFF, 120, (240 - x // 2) & 0xFF))
        with self.assertRaises(FrameNotFoundError):
            decode_frame(_encode_png(gradient, 320, 240))
        canvas = _desktop()
        with self.assertRaises(FrameNotFoundError):
            decode_frame(_encode_png(canvas, 1920, 1080))

    def test_cropped_frame_rejected(self):
        image = encode_frame(_sample_bytes(64), session_id=42)
        width, height, canvas = _place(image, 100, 100)
        cropped_h = 200
        cropped = bytearray(width * cropped_h * 3)
        for y in range(cropped_h):
            source = y * width * 3
            cropped[source:source + width * 3] = canvas[source:source + width * 3]
        with self.assertRaises(VisualCodecError):
            decode_frame(_encode_png(cropped, width, cropped_h))

    def test_ambiguous_frames_rejected(self):
        image = encode_frame(_sample_bytes(64), session_id=43)
        width, height, canvas = _place(image, 100, 100)
        _place(image, 900, 400, size=(width, height), canvas=canvas)
        with self.assertRaises(AmbiguousFrameError):
            decode_frame(_encode_png(canvas, width, height))


class CliTests(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(vc.__file__).resolve().parent.parent)
        return subprocess.run(
            [sys.executable, "-m", "titella.visual_codec", *arguments],
            capture_output=True, env=env, check=False,
        )

    def test_cli_round_trip(self):
        payload = bytes(range(256))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.bin"
            frame = Path(tmp) / "frame.png"
            restored = Path(tmp) / "output.bin"
            source.write_bytes(payload)
            encoded = self._run(
                "encode", str(source), str(frame),
                "--session-id", "7", "--columns", "32", "--cell-size", "6",
            )
            self.assertEqual(encoded.returncode, 0, encoded.stderr)
            decoded = self._run("decode", str(frame), str(restored))
            self.assertEqual(decoded.returncode, 0, decoded.stderr)
            self.assertEqual(restored.read_bytes(), payload)
            self.assertIn(b"session_id=7", decoded.stderr)
            json_run = self._run("decode", str(frame), str(restored), "--json")
            self.assertEqual(json_run.returncode, 0, json_run.stderr)
            info = json.loads(json_run.stderr.decode().strip().splitlines()[-1])
            self.assertEqual(info["session_id"], 7)
            self.assertEqual(info["payload_bytes"], len(payload))
            # Screenshot route through the CLI.
            frame_w, frame_h, frame_rgb = _decode_png(frame.read_bytes())
            width, height, canvas = _place(
                _encode_png(frame_rgb, frame_w, frame_h), 213, 117
            )
            shot = Path(tmp) / "shot.png"
            shot.write_bytes(_encode_png(canvas, width, height))
            shot_out = Path(tmp) / "shot.bin"
            shot_run = self._run("decode", str(shot), str(shot_out))
            self.assertEqual(shot_run.returncode, 0, shot_run.stderr)
            self.assertEqual(shot_out.read_bytes(), payload)
            failure = self._run(
                "encode", str(source), str(frame), "--columns", "8"
            )
            self.assertEqual(failure.returncode, 2)
            self.assertIn(b"error:", failure.stderr)


if __name__ == "__main__":
    unittest.main()
