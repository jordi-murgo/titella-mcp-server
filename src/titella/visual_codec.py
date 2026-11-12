"""Local eight-color matrix frame codec for Titella.

encode_frame() renders a standalone PNG "frame" that carries arbitrary
binary bytes plus fragment metadata; decode_frame() locates that frame
anywhere inside a larger, axis-aligned screenshot and returns the exact
original bytes. The codec is a pure local image transformation: no capture
device, HID, remote or clipboard interaction belongs to this module, and
nothing in the runtime imports it.

Frame geometry (format version 1; sizes in cells unless stated)
- The image is a grid of square cells of ``cell_size`` pixels.
- Margin ring: 2 black cell rings on every side (MARGIN_CELLS = 2).
- Grid row 2 (sync row): ``columns`` cells alternating black (even index),
  white (odd index). Monochrome, self-locating fiducial.
- Grid row 3 (palette row): [black, white, palette colors 0..7, black...]
  per-frame color samples used to classify received cells.
- Grid rows 4..16 (header strip): a fixed 13 rows x 16 columns region
  holding the 200 cells (25 codewords) of the metadata block, independent
  of ``columns``; strip cells 200..207 and strip columns >= 16 stay black.
- Grid rows 17..: payload area, ``data_rows`` rows of ``columns`` cells
  with codewords 25.. laid out row-major; unused trailing cells stay black.
- Image size: width (columns+4)*cell_size, height (data_rows+19)*cell_size.

Palette (cell color index -> RGB)
    0 black (0,0,0)      1 blue (0,0,255)    2 green (0,255,0)
    3 cyan (0,255,255)   4 red (255,0,0)     5 magenta (255,0,255)
    6 yellow (255,255,0) 7 white (255,255,255)
Color index bit 2 comes from bitplane 2, bit 1 from plane 1, bit 0 from
plane 0.

Byte and bit order
- Multi-byte integers are big-endian. Bit g of a byte stream: byte g//8,
  bit (7 - g%8), counted from the start of the stream.
- Stream = metadata block || payload. Metadata block (37 bytes):
      off size field
      0    4    magic "TVF1"
      4    1    format version (1)
      5    1    flags (0)
      6    2    payload_len in bytes
      8    4    session_id
      12   4    sequence
      16   2    fragment_index
      18   2    fragment_count
      20   2    data_columns (the ``columns`` parameter)
      22   2    data_rows
      24   2    cell_size
      26   1    margin cells (2)
      27   1    reserved (0)
      28   1    reserved (0)
      29   4    CRC-32 (zlib polynomial) of metadata bytes 0..28
      33   4    CRC-32 of metadata bytes 0..28 || payload
- Total stream bits = (37 + payload_len) * 8; codeword_count =
  ceil(total / 12); the codeword_count*12 - total trailing bits are zero
  and are checked on decode.

Error correction (extended Hamming(8,4), one codeword per bitplane)
- 12 stream bits b11..b0 form three nibbles: plane 2 = b11..b8,
  plane 1 = b7..b4, plane 0 = b3..b0. Each nibble becomes one 8-bit
  codeword. The 8 cells of a codeword carry codeword bits x1..x8 with
  cell k holding bit x(k+1) (x1 is the most significant codeword bit):
  cell color index = 4 * (bit k of plane-2 codeword)
                  + 2 * (bit k of plane-1 codeword)
                  +     (bit k of plane-0 codeword),  bit k = byte>>(7-k)&1.
- Nibble bits d0..d3 (d0 = LSB) map to codeword bits:
      x1 = d0^d1^d3   x2 = d0^d2^d3   x3 = d0   x4 = d1^d2^d3
      x5 = d1         x6 = d2         x7 = d3
      x8 = x1^x2^x3^x4^x5^x6^x7
- Decoding: s1 = x1^x3^x5^x7, s2 = x2^x3^x6^x7, s4 = x4^x5^x6^x7,
  s8 = x1^x2^x3^x4^x5^x6^x7^x8. All zero: clean. s8 = 1: single-bit error
  at position s1 + 2*s2 + 4*s4 (position 0 means bit x8); flip it. s8 = 0
  with a nonzero syndrome: double error, the codeword is rejected.
- One wrong cell per codeword (any color substitution) is corrected; two
  or more reject the frame. Overhead: 8 cells per 12 data bits (33%). The
  metadata block gets the same ECC plus its own CRC-32; the payload gets
  the ECC plus the packet CRC-32. The decoder never guesses.

Decoder contract and supported degradation
- Inputs: a PNG of at most 8192x8192 pixels and 16,777,216 total pixels;
  the frame must lie fully inside the image. Header geometry is
  revalidated against the encoder bounds before payload work, so a
  corrupt header cannot force large allocations.
- Supported: any axis-aligned placement; integer nearest-neighbor scaling
  of the whole frame by 1x..16x; per-channel drift of roughly gain
  0.7..1.4 with offset -24..+24; additive noise up to +-12 per channel;
  any background. The sync row's black/white luminance separation must
  stay >= ~100.
- Rejected deliberately: rotation, perspective, non-integer or
  interpolated scaling, blur, cropped frames, unknown format versions,
  CRC mismatches, double-cell damage in one codeword, and ambiguous
  images containing more than one frame.
- Detection: image rows with strong luminance transitions are searched
  for runs of equally spaced transitions (the sync row); a candidate
  needs a strictly alternating black/white sync band exactly one cell
  row tall, a palette row with a dark/bright lead pair and eight pairwise
  separated samples, then a magic-valid, CRC-valid header. The detected
  pitch must be an integer multiple of the header cell_size (the scale),
  and the sync row, margins and extents are re-verified against the
  header geometry before the payload is read.
- Errors: FrameNotFoundError (no frame), AmbiguousFrameError (more than
  one candidate), InvalidFrameError (found but corrupt, truncated or
  unknown version), FrameEncodingError (encode-side validation).

Capacity
- payload_len <= 65535 and, for the chosen geometry,
  R_cap = min(1024, 8192 // cell_size - 19) data rows
  max_payload_bytes(columns, cell_size) =
      (((R_cap * columns) // 8 - 25) * 12 - 296) // 8
  while (columns + 4) * cell_size <= 8192. Over that, encode_frame raises.

CLI
    python -m titella.visual_codec encode IN OUT [--session-id N]
        [--sequence N] [--fragment-index N] [--fragment-count N]
        [--columns N] [--cell-size N]
    python -m titella.visual_codec decode IN OUT [--json]
Binary results are written to OUT only; diagnostics go to stderr.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from dataclasses import dataclass

from titella.png_codec import PNG_SIGNATURE, decode_png, encode_png_rgb

FORMAT_MAGIC = b"TVF1"
FORMAT_VERSION = 1
MARGIN_CELLS = 2
GRID_SYNC_ROW = 2
GRID_PALETTE_ROW = 3
GRID_STRIP_ROW = 4
STRIP_COLUMNS = 16
METADATA_BYTES = 37
METADATA_BITS = METADATA_BYTES * 8
METADATA_CODEWORDS = 25  # ceil(296 / 12)
STRIP_CELLS = METADATA_CODEWORDS * 8
STRIP_ROWS = 13  # ceil(200 / 16)
GRID_PAYLOAD_ROW = GRID_STRIP_ROW + STRIP_ROWS
GRID_ROWS_BEFORE_PAYLOAD = GRID_PAYLOAD_ROW + MARGIN_CELLS  # 19

MIN_COLUMNS = 16
MAX_COLUMNS = 256
MIN_CELL_SIZE = 4
MAX_CELL_SIZE = 32
MAX_DATA_ROWS = 1024
MAX_FRAME_SIDE_PX = 8192
MAX_INPUT_SIDE_PX = 8192
MAX_INPUT_PIXELS = 16_777_216
MAX_SCALE = 16
MAX_PAYLOAD_BYTES = 0xFFFF

MIN_ROW_CONTRAST = 80
TRANSITION_DELTA = 40
MIN_SYNC_SEPARATION = 80
MIN_PALETTE_DISTANCE = 48
MIN_PALETTE_DISTANCE2 = MIN_PALETTE_DISTANCE * MIN_PALETTE_DISTANCE

PALETTE: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0),
    (0, 0, 255),
    (0, 255, 0),
    (0, 255, 255),
    (255, 0, 0),
    (255, 0, 255),
    (255, 255, 0),
    (255, 255, 255),
)

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

_LUM_R, _LUM_G, _LUM_B = 77, 151, 28

_HEADER = struct.Struct(">4sBBHIIHHHHHBBB")  # 29 bytes before the two CRCs


class VisualCodecError(Exception):
    """Base class for every visual codec failure."""


class FrameEncodingError(VisualCodecError):
    """encode_frame rejected its arguments or the frame exceeds the caps."""


class FrameNotFoundError(VisualCodecError):
    """No frame was found in the supplied image."""


class AmbiguousFrameError(VisualCodecError):
    """More than one frame candidate verified; the decoder refuses to guess."""


class InvalidFrameError(VisualCodecError):
    """A frame was found but is corrupt, truncated or of unknown version."""


@dataclass(frozen=True)
class DecodedFrame:
    """Immutable result of decode_frame."""

    payload: bytes
    session_id: int
    sequence: int
    fragment_index: int
    fragment_count: int
    columns: int
    cell_size: int
    data_rows: int
    scale: int
    bbox: tuple[int, int, int, int]
    corrected_cells: int
    codeword_count: int


class _EccError(Exception):
    """A codeword carries an uncorrectable error."""


class _FormatError(Exception):
    """Stream-level inconsistency (padding)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _build_hamming_tables() -> tuple[bytes, list[tuple[int, bool] | None]]:
    """Extended Hamming(8,4) encode/decode tables per the module spec."""
    enc = bytearray(16)
    for nibble in range(16):
        d0 = nibble & 1
        d1 = (nibble >> 1) & 1
        d2 = (nibble >> 2) & 1
        d3 = (nibble >> 3) & 1
        x1 = d0 ^ d1 ^ d3
        x2 = d0 ^ d2 ^ d3
        x4 = d1 ^ d2 ^ d3
        x8 = x1 ^ x2 ^ d0 ^ x4 ^ d1 ^ d2 ^ d3
        enc[nibble] = (
            (x1 << 7) | (x2 << 6) | (d0 << 5) | (x4 << 4)
            | (d1 << 3) | (d2 << 2) | (d3 << 1) | x8
        )
    dec: list[tuple[int, bool] | None] = [None] * 256
    for nibble in range(16):
        word = enc[nibble]
        dec[word] = (nibble, False)
        for position in range(8):
            flipped = word ^ (1 << (7 - position))
            dec[flipped] = (nibble, True)
    return bytes(enc), dec


_HAM_ENC, _HAM_DEC = _build_hamming_tables()


def _codeword_count(payload_len: int) -> int:
    total_bits = METADATA_BITS + payload_len * 8
    return -(-total_bits // 12)


def _validate_int(name: str, value: object, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FrameEncodingError(
            f"{name}_type: expected int, got {type(value).__name__}"
        )
    if value < lo or value > hi:
        raise FrameEncodingError(f"{name}_out_of_range: {value} not in [{lo}, {hi}]")
    return value


def _validate_geometry(columns: int, cell_size: int) -> None:
    if not MIN_COLUMNS <= columns <= MAX_COLUMNS:
        raise FrameEncodingError(
            f"columns_out_of_range: {columns} not in [{MIN_COLUMNS}, {MAX_COLUMNS}]"
        )
    if not MIN_CELL_SIZE <= cell_size <= MAX_CELL_SIZE:
        raise FrameEncodingError(
            f"cell_size_out_of_range: {cell_size} not in "
            f"[{MIN_CELL_SIZE}, {MAX_CELL_SIZE}]"
        )
    width_px = (columns + 2 * MARGIN_CELLS) * cell_size
    if width_px > MAX_FRAME_SIDE_PX:
        raise FrameEncodingError(
            f"frame_too_large: width {width_px} exceeds {MAX_FRAME_SIDE_PX}"
        )


def max_payload_bytes(columns: int, cell_size: int) -> int:
    """Largest payload encodable with the given geometry (see docstring)."""
    _validate_geometry(columns, cell_size)
    rows_cap = min(MAX_DATA_ROWS, MAX_FRAME_SIDE_PX // cell_size - GRID_ROWS_BEFORE_PAYLOAD)
    if rows_cap < 0:
        return 0
    payload_codewords_cap = (rows_cap * columns) // 8
    if payload_codewords_cap <= 0:
        return 0
    bits = (payload_codewords_cap + METADATA_CODEWORDS) * 12 - METADATA_BITS
    return min(bits // 8, MAX_PAYLOAD_BYTES)


def _stream_to_cells(stream: bytes, codeword_count: int) -> list[int]:
    """Maps the ECC stream to cell color indices (see module spec)."""
    stream_bits = len(stream) * 8

    def bit(index: int) -> int:
        if index >= stream_bits:
            return 0
        return (stream[index >> 3] >> (7 - (index & 7))) & 1

    def nibble(base: int) -> int:
        value = 0
        for k in range(4):
            value = (value << 1) | bit(base + k)
        return value

    cells: list[int] = []
    for codeword in range(codeword_count):
        base = codeword * 12
        # Plane p (p = 2, 1, 0) reads stream bits base + (2-p)*4 .. +3.
        e2 = _HAM_ENC[nibble(base)]
        e1 = _HAM_ENC[nibble(base + 4)]
        e0 = _HAM_ENC[nibble(base + 8)]
        for k in range(8):
            shift = 7 - k
            cells.append(
                (((e2 >> shift) & 1) << 2)
                | (((e1 >> shift) & 1) << 1)
                | ((e0 >> shift) & 1)
            )
    return cells


def _cells_to_bits(cells: list[int], codeword_count: int) -> tuple[list[int], int]:
    """Inverse of _stream_to_cells; raises _EccError on double errors."""
    bits: list[int] = []
    corrected = 0
    for codeword in range(codeword_count):
        e2 = e1 = e0 = 0
        for k in range(8):
            color = cells[codeword * 8 + k]
            shift = 7 - k
            e2 |= ((color >> 2) & 1) << shift
            e1 |= ((color >> 1) & 1) << shift
            e0 |= (color & 1) << shift
        nibbles = []
        for word in (e2, e1, e0):
            entry = _HAM_DEC[word]
            if entry is None:
                raise _EccError(f"uncorrectable codeword {codeword}")
            nibbles.append(entry)
        if nibbles[0][1] or nibbles[1][1] or nibbles[2][1]:
            corrected += 1
        for nibble, _ in nibbles:
            bits.extend((nibble >> shift) & 1 for shift in (3, 2, 1, 0))
    return bits, corrected


def _bits_to_stream(bits: list[int], stream_bits: int, total_bits: int) -> bytes:
    """Packs bits MSB-first; trailing pad bits must be zero."""
    for index in range(stream_bits, total_bits):
        if bits[index]:
            raise _FormatError("padding_mismatch")
    out = bytearray(stream_bits // 8)
    for index in range(stream_bits):
        if bits[index]:
            out[index >> 3] |= 1 << (7 - (index & 7))
    return bytes(out)


def _render(
    cells: list[int], columns: int, data_rows: int, cell_size: int
) -> tuple[bytes, int, int]:
    """Paints the cell grid into an RGB buffer plus its pixel size."""
    grid_cols = columns + 2 * MARGIN_CELLS
    grid_rows = data_rows + GRID_ROWS_BEFORE_PAYLOAD
    blocks = [bytes(color) * cell_size for color in PALETTE]
    black = bytes(BLACK) * cell_size
    white = bytes(WHITE) * cell_size
    buf = bytearray()
    for grid_row in range(grid_rows):
        row_blocks = [black] * grid_cols  # margins and filler by default
        if grid_row == GRID_SYNC_ROW:
            for j in range(columns):
                row_blocks[MARGIN_CELLS + j] = black if j % 2 == 0 else white
        elif grid_row == GRID_PALETTE_ROW:
            row_blocks[MARGIN_CELLS] = black
            row_blocks[MARGIN_CELLS + 1] = white
            for i in range(8):
                row_blocks[MARGIN_CELLS + 2 + i] = blocks[i]
        elif GRID_STRIP_ROW <= grid_row < GRID_PAYLOAD_ROW:
            for c in range(min(STRIP_COLUMNS, columns)):
                t = (grid_row - GRID_STRIP_ROW) * STRIP_COLUMNS + c
                if t < len(cells):
                    row_blocks[MARGIN_CELLS + c] = blocks[cells[t]]
        elif GRID_PAYLOAD_ROW <= grid_row < grid_rows - MARGIN_CELLS:
            base = (grid_row - GRID_PAYLOAD_ROW) * columns
            for c in range(columns):
                t = STRIP_CELLS + base + c
                if t < len(cells):
                    row_blocks[MARGIN_CELLS + c] = blocks[cells[t]]
        buf += b"".join(row_blocks) * cell_size
    width = grid_cols * cell_size
    height = grid_rows * cell_size
    return bytes(buf), width, height


def encode_frame(
    payload: bytes,
    *,
    session_id: int,
    sequence: int = 0,
    fragment_index: int = 0,
    fragment_count: int = 1,
    columns: int = 48,
    cell_size: int = 8,
) -> bytes:
    """Encodes payload plus metadata into a standalone frame PNG."""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        payload = bytes(payload)
    else:
        raise FrameEncodingError(
            f"payload_type: expected bytes-like, got {type(payload).__name__}"
        )
    _validate_int("session_id", session_id, 0, 0xFFFFFFFF)
    _validate_int("sequence", sequence, 0, 0xFFFFFFFF)
    _validate_int("fragment_count", fragment_count, 1, 0xFFFF)
    _validate_int("fragment_index", fragment_index, 0, fragment_count - 1)
    _validate_geometry(columns, cell_size)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise FrameEncodingError(
            f"payload_too_large: {len(payload)} bytes exceeds {MAX_PAYLOAD_BYTES}"
        )
    codewords = _codeword_count(len(payload))
    payload_cells = (codewords - METADATA_CODEWORDS) * 8
    data_rows = -(-payload_cells // columns) if payload_cells else 0
    if data_rows > MAX_DATA_ROWS:
        raise FrameEncodingError(
            f"frame_too_large: needs {data_rows} data rows, cap is {MAX_DATA_ROWS}"
        )
    height_px = (data_rows + GRID_ROWS_BEFORE_PAYLOAD) * cell_size
    if height_px > MAX_FRAME_SIDE_PX:
        raise FrameEncodingError(
            f"frame_too_large: height {height_px} exceeds {MAX_FRAME_SIDE_PX}"
        )
    header = _HEADER.pack(
        FORMAT_MAGIC, FORMAT_VERSION, 0, len(payload), session_id, sequence,
        fragment_index, fragment_count, columns, data_rows, cell_size,
        MARGIN_CELLS, 0, 0,
    )
    header_crc = zlib.crc32(header) & 0xFFFFFFFF
    packet_crc = zlib.crc32(header + payload) & 0xFFFFFFFF
    stream = header + struct.pack(">II", header_crc, packet_crc) + payload
    cells = _stream_to_cells(stream, codewords)
    rgb, width, height = _render(cells, columns, data_rows, cell_size)
    return encode_png_rgb(width, height, rgb)


def _luminance(pixel: tuple[int, int, int]) -> int:
    return (_LUM_R * pixel[0] + _LUM_G * pixel[1] + _LUM_B * pixel[2]) >> 8


def _sample(rgb: bytes, width: int, height: int, x: int, y: int):
    if 0 <= x < width and 0 <= y < height:
        index = (y * width + x) * 3
        return rgb[index], rgb[index + 1], rgb[index + 2]
    return None


def _row_transitions(row: bytes) -> list[int]:
    """Strong luminance transitions of one RGB scanline, in one pass."""
    total = len(row) // 3
    out: list[int] = []
    previous = (_LUM_R * row[0] + _LUM_G * row[1] + _LUM_B * row[2]) >> 8
    for x in range(1, total):
        i = x * 3
        current = (_LUM_R * row[i] + _LUM_G * row[i + 1] + _LUM_B * row[i + 2]) >> 8
        delta = previous - current
        if delta > TRANSITION_DELTA or -delta > TRANSITION_DELTA:
            out.append(x)
        previous = current
    return out


def _pitch_runs(positions: list[int]) -> list[tuple[int, list[int]]]:
    """Maximal runs of near-equally spaced transition positions."""
    runs: list[tuple[int, list[int]]] = []
    total = len(positions)
    i = 0
    while i < total:
        base = None
        j = i
        while j + 1 < total:
            gap = positions[j + 1] - positions[j]
            if base is None:
                if gap < MIN_CELL_SIZE:
                    break
                base = gap
                tol = 2 + gap // 64
            elif abs(gap - base) > tol:
                break
            j += 1
        if base is not None and j > i:
            gaps = j - i
            pitch = (2 * (positions[j] - positions[i]) + gaps) // (2 * gaps)
            if pitch >= MIN_CELL_SIZE and gaps + 1 >= MIN_COLUMNS - 1:
                runs.append((pitch, positions[i:j + 1]))
        i = j + 1 if j > i else i + 1
    return runs


def _sync_probe(
    rgb: bytes, width: int, height: int, x_sync: int, y: int, pitch: int
) -> tuple[int, int] | None:
    """Checks the sync alternation on row y; returns dark/bright thresholds."""
    samples = []
    for j in range(8):
        pixel = _sample(
            rgb, width, height, int(x_sync + (j + 0.5) * pitch), y
        )
        if pixel is None:
            return None
        samples.append(_luminance(pixel))
    dark = min(samples[0], samples[2], samples[4], samples[6])
    bright = max(samples[1], samples[3], samples[5], samples[7])
    if bright - dark < MIN_SYNC_SEPARATION:
        return None
    dark_thr = dark + (bright - dark) // 4
    bright_thr = bright - (bright - dark) // 4
    for j, value in enumerate(samples):
        if j % 2 == 0:
            if value > dark_thr:
                return None
        elif value < bright_thr:
            return None
    return dark_thr, bright_thr


def _probe_pass(
    rgb: bytes, width: int, height: int,
    x_sync: int, y: int, pitch: int, dark_thr: int, bright_thr: int,
) -> bool:
    for j in range(8):
        pixel = _sample(
            rgb, width, height, int(x_sync + (j + 0.5) * pitch), y
        )
        if pixel is None:
            return False
        value = _luminance(pixel)
        if j % 2 == 0:
            if value > dark_thr:
                return False
        elif value < bright_thr:
            return False
    return True


def _sync_band_top(
    rgb: bytes, width: int, height: int,
    x_sync: int, y: int, pitch: int, dark_thr: int, bright_thr: int,
) -> int | None:
    """Walks up/down; the sync band must be exactly one cell row tall."""
    up = 0
    probe_y = y - 1
    while up < pitch + 1 and probe_y >= 0 and _probe_pass(
        rgb, width, height, x_sync, probe_y, pitch, dark_thr, bright_thr
    ):
        up += 1
        probe_y -= 1
    down = 0
    probe_y = y + 1
    while down < pitch + 1 and probe_y < height and _probe_pass(
        rgb, width, height, x_sync, probe_y, pitch, dark_thr, bright_thr
    ):
        down += 1
        probe_y += 1
    if 1 + up + down != pitch:
        return None
    return y - up


def _min_palette_distance2(palette: list[tuple[int, int, int]]) -> int:
    best = None
    for i in range(len(palette)):
        for j in range(i + 1, len(palette)):
            a, b = palette[i], palette[j]
            distance = (
                (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
            )
            if best is None or distance < best:
                best = distance
    return best if best is not None else 0


def _make_classifier(palette: list[tuple[int, int, int]]):
    """Nearest-sample classifier with a quantized decision cache."""
    cache: dict[tuple[int, int, int], int] = {}

    def classify(pixel: tuple[int, int, int]) -> int:
        key = (pixel[0] >> 3, pixel[1] >> 3, pixel[2] >> 3)
        cached = cache.get(key)
        if cached is None:
            r, g, b = pixel
            best_index = -1
            best = second = None
            for index, (pr, pg, pb) in enumerate(palette):
                distance = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
                if best is None or distance < best:
                    second = best
                    best = distance
                    best_index = index
                elif second is None or distance < second:
                    second = distance
            if best is not None and best == second:
                best_index = -1
            cached = best_index
            cache[key] = cached
        return cached

    return classify


def _structure_mismatch(
    rgb: bytes, width: int, height: int,
    x_sync: int, y_top: int, pitch: int,
    columns: int, data_rows: int,
    dark_thr: int, bright_thr: int,
) -> bool:
    """Re-verifies the sync row and the black margins against the header."""
    sync_y = int(y_top + 0.5 * pitch)
    for j in range(columns):
        pixel = _sample(
            rgb, width, height, int(x_sync + (j + 0.5) * pitch), sync_y
        )
        if pixel is None:
            return True
        value = _luminance(pixel)
        if j % 2 == 0:
            if value > dark_thr:
                return True
        elif value < bright_thr:
            return True
    probes = (
        (int(x_sync - 0.5 * pitch), sync_y),
        (int(x_sync + (columns + 0.5) * pitch), sync_y),
        (int(x_sync + 0.5 * pitch), int(y_top - 0.5 * pitch)),
        (int(x_sync + 0.5 * pitch), int(y_top + (data_rows + 16.5) * pitch)),
    )
    for x, y in probes:
        pixel = _sample(rgb, width, height, x, y)
        if pixel is None or _luminance(pixel) > dark_thr:
            return True
    return False


def _decode_candidate(
    rgb: bytes, width: int, height: int,
    x_sync: int, y_top: int, pitch: int,
    dark_thr: int, bright_thr: int,
):
    """Verifies one candidate location end to end.

    Returns ("frame", DecodedFrame), ("corrupt", (bbox|None, reason)) or
    ("miss", None) when the candidate is not a TVF frame at all.
    """
    palette_y = int(y_top + 1.5 * pitch)
    samples = []
    for j in range(10):
        pixel = _sample(
            rgb, width, height, int(x_sync + (j + 0.5) * pitch), palette_y
        )
        if pixel is None:
            return "miss", None
        samples.append(pixel)
    if _luminance(samples[0]) > dark_thr or _luminance(samples[1]) < bright_thr:
        return "miss", None
    palette = samples[2:]
    if _min_palette_distance2(palette) < MIN_PALETTE_DISTANCE2:
        return "miss", None
    classify = _make_classifier(palette)

    strip: list[int] = []
    for t in range(STRIP_CELLS):
        pixel = _sample(
            rgb, width, height,
            int(x_sync + (t % STRIP_COLUMNS + 0.5) * pitch),
            int(y_top + (t // STRIP_COLUMNS + 2.5) * pitch),
        )
        if pixel is None:
            return "miss", None
        index = classify(pixel)
        if index < 0:
            return "miss", None
        strip.append(index)
    try:
        bits, corrected = _cells_to_bits(strip, METADATA_CODEWORDS)
        # The metadata block is exactly 296 bits; bits beyond it belong to the
        # payload stream and are not padding, so no pad check applies here.
        meta = _bits_to_stream(bits, METADATA_BITS, METADATA_BITS)
    except (_EccError, _FormatError):
        return "miss", None
    if meta[:4] != FORMAT_MAGIC:
        return "miss", None

    (
        _magic, version, flags, payload_len, session_id, sequence,
        fragment_index, fragment_count, columns, data_rows, cell_size,
        margin, reserved1, reserved2,
    ) = _HEADER.unpack_from(meta, 0)
    header_crc_field, packet_crc_field = struct.unpack_from(">II", meta, 29)

    def corrupt(reason: str, bbox=None):
        return "corrupt", (bbox, reason)

    if version != FORMAT_VERSION:
        return corrupt("unsupported_format_version")
    if zlib.crc32(meta[:29]) & 0xFFFFFFFF != header_crc_field:
        return corrupt("header_checksum_mismatch")
    if flags != 0 or reserved1 != 0 or reserved2 != 0 or margin != MARGIN_CELLS:
        return corrupt("invalid_header")
    if not MIN_COLUMNS <= columns <= MAX_COLUMNS:
        return corrupt("invalid_geometry")
    if not 0 <= data_rows <= MAX_DATA_ROWS:
        return corrupt("invalid_geometry")
    if not MIN_CELL_SIZE <= cell_size <= MAX_CELL_SIZE:
        return corrupt("invalid_geometry")
    if not 1 <= fragment_count <= 0xFFFF or fragment_index >= fragment_count:
        return corrupt("invalid_metadata")
    codewords = _codeword_count(payload_len)
    payload_cells = (codewords - METADATA_CODEWORDS) * 8
    expected_rows = -(-payload_cells // columns) if payload_cells else 0
    if data_rows != expected_rows:
        return corrupt("geometry_mismatch")
    if pitch % cell_size:
        return corrupt("scale_mismatch")
    scale = pitch // cell_size
    if not 1 <= scale <= MAX_SCALE:
        return corrupt("scale_mismatch")
    x_grid = x_sync - MARGIN_CELLS * pitch
    y_grid = y_top - MARGIN_CELLS * pitch
    w_grid = (columns + 2 * MARGIN_CELLS) * pitch
    h_grid = (data_rows + GRID_ROWS_BEFORE_PAYLOAD) * pitch
    if (
        x_grid < 0 or y_grid < 0
        or x_grid + w_grid > width or y_grid + h_grid > height
    ):
        return corrupt("frame_truncated", (x_grid, y_grid, w_grid, h_grid))
    bbox = (x_grid, y_grid, w_grid, h_grid)
    if _structure_mismatch(
        rgb, width, height, x_sync, y_top, pitch, columns, data_rows,
        dark_thr, bright_thr,
    ):
        return corrupt("structure_mismatch", bbox)

    payload_color_cells: list[int] = []
    for q in range(payload_cells):
        pixel = _sample(
            rgb, width, height,
            int(x_sync + (q % columns + 0.5) * pitch),
            int(y_top + (q // columns + 15.5) * pitch),
        )
        if pixel is None:
            return corrupt("frame_truncated", bbox)
        index = classify(pixel)
        if index < 0:
            return corrupt("ambiguous_cell", bbox)
        payload_color_cells.append(index)
    try:
        payload_bits, payload_corrected = _cells_to_bits(
            payload_color_cells, codewords - METADATA_CODEWORDS
        )
        stream = _bits_to_stream(
            bits + payload_bits,
            METADATA_BITS + payload_len * 8,
            codewords * 12,
        )
    except _EccError:
        return corrupt("payload_ecc_failure", bbox)
    except _FormatError as exc:
        return corrupt(exc.reason, bbox)
    if (
        zlib.crc32(stream[:29] + stream[METADATA_BYTES:]) & 0xFFFFFFFF
        != packet_crc_field
    ):
        return corrupt("checksum_mismatch", bbox)
    return "frame", DecodedFrame(
        payload=stream[METADATA_BYTES:],
        session_id=session_id,
        sequence=sequence,
        fragment_index=fragment_index,
        fragment_count=fragment_count,
        columns=columns,
        cell_size=cell_size,
        data_rows=data_rows,
        scale=scale,
        bbox=bbox,
        corrected_cells=corrected + payload_corrected,
        codeword_count=codewords,
    )


def _process_candidate(
    rgb: bytes, width: int, height: int,
    x_sync: int, y: int, pitch: int,
    seen: set, rejected: set,
):
    probe = _sync_probe(rgb, width, height, x_sync, y, pitch)
    if probe is None:
        return None
    dark_thr, bright_thr = probe
    y_top = _sync_band_top(
        rgb, width, height, x_sync, y, pitch, dark_thr, bright_thr
    )
    if y_top is None:
        return None
    key = (x_sync, pitch, y_top)
    if key in seen or key in rejected:
        return None
    outcome = _decode_candidate(
        rgb, width, height, x_sync, y_top, pitch, dark_thr, bright_thr
    )
    if outcome[0] == "frame" or outcome[0] == "corrupt":
        seen.add(key)
        return outcome
    rejected.add(key)
    return None


def _locate(rgb: bytes, width: int, height: int):
    frames: list[DecodedFrame] = []
    corrupt: list[tuple[tuple[int, int, int, int] | None, str]] = []
    seen: set = set()
    rejected: set = set()
    row_seen: set = set()
    for y in range(height):
        base = y * width * 3
        row = rgb[base:base + width * 3]
        if not row:
            continue
        if max(row) - min(row) < MIN_ROW_CONTRAST:
            continue
        positions = _row_transitions(row)
        if len(positions) < MIN_COLUMNS - 1:
            continue
        row_seen.clear()
        for pitch, run in _pitch_runs(positions):
            for index in range(len(run)):
                first = run[index]
                for x_sync in (first - pitch, first - 2 * pitch):
                    if x_sync < 0:
                        continue
                    key = (x_sync, pitch, y)
                    if key in seen or key in rejected or key in row_seen:
                        continue
                    outcome = _process_candidate(
                        rgb, width, height, x_sync, y, pitch, seen, rejected
                    )
                    if outcome is not None:
                        row_seen.add(key)
                        kind, value = outcome
                        if kind == "frame":
                            frames.append(value)
                        else:
                            corrupt.append(value)
    return frames, corrupt


def _validate_input_png(data: bytes) -> None:
    if len(data) < 33:
        raise InvalidFrameError(f"truncated_png: {len(data)} bytes is too short")
    if data[:8] != PNG_SIGNATURE:
        raise InvalidFrameError("not_png: missing PNG signature")
    width, height = struct.unpack_from(">II", data, 16)
    if width < 1 or height < 1:
        raise InvalidFrameError(f"invalid_png: dimensions {width}x{height}")
    if width > MAX_INPUT_SIDE_PX or height > MAX_INPUT_SIDE_PX:
        raise InvalidFrameError(
            f"input_too_large: {width}x{height} exceeds {MAX_INPUT_SIDE_PX}"
        )
    if width * height > MAX_INPUT_PIXELS:
        raise InvalidFrameError(
            f"input_too_large: {width * height} pixels exceeds {MAX_INPUT_PIXELS}"
        )


def decode_frame(png: bytes) -> DecodedFrame:
    """Locates and decodes one frame from a PNG or screenshot image."""
    if not isinstance(png, (bytes, bytearray, memoryview)):
        raise InvalidFrameError(
            f"input_type: expected PNG bytes, got {type(png).__name__}"
        )
    data = bytes(png)
    _validate_input_png(data)
    try:
        width, height, rgb = decode_png(data)
    except ValueError as exc:
        raise InvalidFrameError(f"invalid_png: {exc}") from None
    frames, corrupt = _locate(rgb, width, height)
    if len(frames) == 1 and not corrupt:
        return frames[0]
    if frames:
        raise AmbiguousFrameError(
            f"ambiguous_frame: {len(frames)} verifiable and "
            f"{len(corrupt)} corrupt candidates"
        )
    if corrupt:
        details = "; ".join(f"{bbox}: {reason}" for bbox, reason in corrupt)
        raise InvalidFrameError(f"frame_corrupt: {details}")
    raise FrameNotFoundError("frame_not_found: no frame located in the image")


def _read_file(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m titella.visual_codec",
        description="Local eight-color matrix frame codec.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    encode = sub.add_parser(
        "encode", help="encode a binary file into a frame PNG"
    )
    encode.add_argument("input")
    encode.add_argument("output")
    encode.add_argument("--session-id", type=int, default=0)
    encode.add_argument("--sequence", type=int, default=0)
    encode.add_argument("--fragment-index", type=int, default=0)
    encode.add_argument("--fragment-count", type=int, default=1)
    encode.add_argument("--columns", type=int, default=48)
    encode.add_argument("--cell-size", type=int, default=8)
    decode = sub.add_parser(
        "decode", help="decode a frame PNG or screenshot into a binary file"
    )
    decode.add_argument("input")
    decode.add_argument("output")
    decode.add_argument(
        "--json", action="store_true", help="print diagnostics as JSON"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "encode":
            payload = _read_file(args.input)
            image = encode_frame(
                payload,
                session_id=args.session_id,
                sequence=args.sequence,
                fragment_index=args.fragment_index,
                fragment_count=args.fragment_count,
                columns=args.columns,
                cell_size=args.cell_size,
            )
            _write_file(args.output, image)
            print(
                f"encoded {len(payload)} bytes into {len(image)}-byte PNG",
                file=sys.stderr,
            )
        else:
            image = _read_file(args.input)
            frame = decode_frame(image)
            _write_file(args.output, frame.payload)
            if args.json:
                print(
                    json.dumps(
                        {
                            "payload_bytes": len(frame.payload),
                            "session_id": frame.session_id,
                            "sequence": frame.sequence,
                            "fragment_index": frame.fragment_index,
                            "fragment_count": frame.fragment_count,
                            "columns": frame.columns,
                            "cell_size": frame.cell_size,
                            "data_rows": frame.data_rows,
                            "scale": frame.scale,
                            "bbox": list(frame.bbox),
                            "corrected_cells": frame.corrected_cells,
                            "codeword_count": frame.codeword_count,
                        }
                    ),
                    file=sys.stderr,
                )
            else:
                print(
                    f"decoded {len(frame.payload)} bytes "
                    f"session_id={frame.session_id} sequence={frame.sequence} "
                    f"fragment={frame.fragment_index}/{frame.fragment_count} "
                    f"bbox={frame.bbox} scale={frame.scale} "
                    f"corrected_cells={frame.corrected_cells}",
                    file=sys.stderr,
                )
    except VisualCodecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
