"""Pure-Python PNG codec for the UVC auto-crop and the screen diff.

Only ``zlib`` and ``struct`` from the standard library are used, so the
master needs no imaging dependency. ``decode_png`` accepts 8-bit images of
color types 0 (grayscale), 2 (RGB), 3 (palette), 4 (grayscale+alpha) and 6
(RGBA) with all five scanline filters and returns 3-bytes-per-pixel RGB:
alpha and palette transparency are dropped because every consumer here
compares pixels, not composites.

Rows are reconstructed with memoryview slices and lane arithmetic. The
``Up``, ``Sub`` and ``None`` filters — the ones real capture cards and
desktop encoders emit — take C-level paths; ``Average`` and ``Paeth`` fall
back to a per-byte loop, which is still exact but slower on a 1080p frame.
"""
from __future__ import annotations

import struct
import zlib
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

_LOW7 = b"\x7f"
_HIGH = b"\x80"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _repeat(unit: bytes, count: int) -> int:
    """Little-endian integer whose bytes are ``unit`` repeated ``count`` times."""
    return int.from_bytes(unit * count, "little")


def _add_bytes_mod256(left: int, right: int, count: int) -> int:
    """Byte-wise ``(left + right) & 0xFF`` without inter-byte carries.

    Adding the two ``0x7F``-masked halves cannot carry across a byte, and the
    ``0x80`` bits of the operands sum to a ``0x80`` exactly where they differ.
    """
    low_mask = _repeat(_LOW7, count)
    high_mask = _repeat(_HIGH, count)
    total = (left & low_mask) + (right & low_mask)
    return total ^ ((left ^ right) & high_mask)


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """Decodes a PNG into ``(width, height, rgb)`` with 3 bytes per pixel.

    Raises ``ValueError`` for anything unsupported or corrupt: a wrong
    signature, a missing IHDR/IEND, a bit depth other than 8, an interlaced
    stream, a missing palette or truncated pixel data.
    """
    view = memoryview(data)
    if len(view) < 8 or view[:8].tobytes() != PNG_SIGNATURE:
        raise ValueError("not a PNG stream")
    position = 8
    width = height = bit_depth = color_type = interlace = -1
    palette: bytes | None = None
    compressed = bytearray()
    saw_identity = False
    while position + 8 <= len(view):
        (length,) = struct.unpack_from(">I", view, position)
        kind = view[position + 4:position + 8].tobytes()
        position += 8
        end = position + length
        if end + 4 > len(view):
            raise ValueError(f"truncated {kind!r} chunk")
        payload = view[position:end].tobytes()
        position = end + 4  # the CRC is not verified: structural checks follow.
        if kind == b"IHDR":
            if length != 13:
                raise ValueError("malformed IHDR chunk")
            (
                width,
                height,
                bit_depth,
                color_type,
                _compression,
                _filter,
                interlace,
            ) = struct.unpack(">IIBBBBB", payload)
            saw_identity = True
        elif kind == b"PLTE":
            palette = payload
        elif kind == b"IDAT":
            compressed += payload
        elif kind == b"IEND":
            break
    if not saw_identity:
        raise ValueError("missing IHDR chunk")
    if width < 1 or height < 1:
        raise ValueError("invalid image dimensions")
    if bit_depth != 8:
        raise ValueError(f"unsupported bit depth {bit_depth}")
    if color_type not in _CHANNELS:
        raise ValueError(f"unsupported color type {color_type}")
    if interlace != 0:
        raise ValueError("interlaced PNG is not supported")
    if color_type == 3 and palette is None:
        raise ValueError("palette image without a PLTE chunk")
    if not compressed:
        raise ValueError("missing IDAT chunk")

    channels = _CHANNELS[color_type]
    row_bytes = width * channels
    scanlines = height * (row_bytes + 1)
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ValueError(f"corrupt image data: {exc}") from exc
    if len(raw) < scanlines:
        raise ValueError("truncated pixel data")

    rows = _unfilter(raw, height, row_bytes, channels)
    rgb = _to_rgb(rows, width, height, color_type, palette)
    return width, height, rgb


def _unfilter(raw: bytes, height: int, row_bytes: int, channels: int) -> list[bytearray]:
    rows: list[bytearray] = []
    previous = bytearray(row_bytes)
    position = 0
    for _ in range(height):
        filter_type = raw[position]
        position += 1
        current = bytearray(raw[position:position + row_bytes])
        position += row_bytes
        if filter_type == 0:
            pass
        elif filter_type == 1:
            current = _sub_unfilter(current, channels)
        elif filter_type == 2:
            left = int.from_bytes(current, "little")
            up = int.from_bytes(previous, "little")
            current = bytearray(
                _add_bytes_mod256(left, up, row_bytes).to_bytes(row_bytes, "little")
            )
        elif filter_type == 3:
            for index in range(row_bytes):
                left_byte = current[index - channels] if index >= channels else 0
                current[index] = (
                    current[index] + ((left_byte + previous[index]) >> 1)
                ) & 0xFF
        elif filter_type == 4:
            for index in range(row_bytes):
                left_byte = current[index - channels] if index >= channels else 0
                up_byte = previous[index]
                up_left = previous[index - channels] if index >= channels else 0
                current[index] = (current[index] + _paeth(left_byte, up_byte, up_left)) & 0xFF
        else:
            raise ValueError(f"unsupported scanline filter {filter_type}")
        rows.append(current)
        previous = current
    return rows


def _sub_unfilter(current: bytearray, channels: int) -> bytearray:
    """Prefixes each channel with its running sum (mod 256).

    The recurrence is per channel, so each channel is scanned independently
    and written back at its interleaved positions.
    """
    count = len(current)
    if count == 0:
        return current
    result = bytearray(count)
    for channel in range(channels):
        result[channel::channels] = _prefix_sum_mod256(bytes(current[channel::channels]))
    return result


def _prefix_sum_mod256(values: bytes) -> bytes:
    """Inclusive prefix sums of a byte string, each byte taken mod 256.

    Hillis-Steele doubling: after step ``d`` every byte holds the sum of the
    ``2d`` bytes ending at it, and the add is carry-free per byte.
    """
    count = len(values)
    if count == 0:
        return b""
    total = int.from_bytes(values, "little")
    width_mask = (1 << (8 * count)) - 1
    stride = 1
    while stride < count:
        shifted = (total << (8 * stride)) & width_mask
        total = _add_bytes_mod256(total, shifted, count)
        stride *= 2
    return total.to_bytes(count, "little")


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_up_left = abs(estimate - up_left)
    if distance_left <= distance_up and distance_left <= distance_up_left:
        return left
    if distance_up <= distance_up_left:
        return up
    return up_left


def _to_rgb(
    rows: list[bytearray],
    width: int,
    height: int,
    color_type: int,
    palette: bytes | None,
) -> bytes:
    if color_type == 2:
        return b"".join(bytes(row) for row in rows)
    rgb = bytearray(width * height * 3)
    if color_type == 0:
        for index, row in enumerate(rows):
            start = index * width * 3
            rgb[start:start + width * 3:3] = row
            rgb[start + 1:start + width * 3:3] = row
            rgb[start + 2:start + width * 3:3] = row
        return bytes(rgb)
    if color_type == 4:
        for index, row in enumerate(rows):
            gray = row[0::2]
            start = index * width * 3
            rgb[start:start + width * 3:3] = gray
            rgb[start + 1:start + width * 3:3] = gray
            rgb[start + 2:start + width * 3:3] = gray
        return bytes(rgb)
    if color_type == 6:
        for index, row in enumerate(rows):
            start = index * width * 3
            rgb[start:start + width * 3:3] = row[0::4]
            rgb[start + 1:start + width * 3:3] = row[1::4]
            rgb[start + 2:start + width * 3:3] = row[2::4]
        return bytes(rgb)
    assert palette is not None  # color_type 3 is validated before conversion
    entries = len(palette) // 3
    lookup = [palette[index * 3:index * 3 + 3] for index in range(entries)]
    for index, row in enumerate(rows):
        try:
            row_rgb = b"".join(map(lookup.__getitem__, row))
        except IndexError as exc:
            raise ValueError(f"palette index out of range: {exc}") from exc
        start = index * width * 3
        rgb[start:start + width * 3] = row_rgb
    return bytes(rgb)


def encode_png_rgb(width: int, height: int, rgb: bytes | bytearray, level: int = 6) -> bytes:
    """Encodes 3-bytes-per-pixel RGB into a PNG (color type 2, no filter)."""
    if width < 1 or height < 1:
        raise ValueError("invalid image dimensions")
    row_bytes = width * 3
    data = bytes(rgb)
    if len(data) != row_bytes * height:
        raise ValueError(f"expected {row_bytes * height} RGB bytes, got {len(data)}")
    filtered = bytearray(height * (row_bytes + 1))
    for row in range(height):
        start = row * (row_bytes + 1)
        filtered[start + 1:start + 1 + row_bytes] = data[row * row_bytes:(row + 1) * row_bytes]
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(filtered), level))
        + _chunk(b"IEND", b"")
    )


def crop_rgb(
    width: int,
    height: int,
    rgb: bytes | bytearray,
    x: int,
    y: int,
    crop_width: int,
    crop_height: int,
) -> bytes:
    """Extracts one rectangle of an RGB buffer; out-of-bounds input is refused."""
    if crop_width < 1 or crop_height < 1:
        raise ValueError("the crop rectangle must have a positive size")
    if x < 0 or y < 0 or x + crop_width > width or y + crop_height > height:
        raise ValueError("the crop rectangle exceeds the image bounds")
    stride = width * 3
    rows: list[bytes] = []
    for row in range(y, y + crop_height):
        start = row * stride + x * 3
        rows.append(bytes(rgb[start:start + crop_width * 3]))
    return b"".join(rows)


def _bright_table(threshold: int) -> bytes:
    return bytes(1 if value > threshold else 0 for value in range(256))


def detect_black_borders(
    width: int,
    height: int,
    rgb: bytes | bytearray,
    threshold: int = 20,
    min_border: int = 4,
    min_content: int = 64,
) -> tuple[int, int, int, int] | None:
    """Finds the active content box inside pillarbox/letterbox black borders.

    A row or column is black when every channel stays at or below
    ``threshold``. The box is only returned when the content is at least
    ``min_content`` pixels on both axes — an all-black "no signal" frame, or a
    tiny speck, is never cropped — and when at least one border is at least
    ``min_border`` pixels thick.
    """
    stride = width * 3
    if len(rgb) < stride * height:
        raise ValueError("the RGB buffer is smaller than the image dimensions")
    table = _bright_table(threshold)

    def row_bright(row: int) -> bool:
        start = row * stride
        return rgb[start:start + stride].translate(table).count(1) > 0

    first_row = next((row for row in range(height) if row_bright(row)), None)
    if first_row is None:
        return None
    last_row = next(row for row in range(height - 1, first_row - 1, -1) if row_bright(row))

    first_column = width
    last_column = -1
    for row in range(first_row, last_row + 1):
        start = row * stride
        translated = rgb[start:start + stride].translate(table)
        head = translated.find(1)
        if head < 0:
            continue
        tail = translated.rfind(1)
        first_column = min(first_column, head // 3)
        last_column = max(last_column, tail // 3)
    if last_column < first_column:
        return None

    content_width = last_column - first_column + 1
    content_height = last_row - first_row + 1
    if content_width < min_content or content_height < min_content:
        return None
    borders = (
        first_column,
        first_row,
        width - (first_column + content_width),
        height - (first_row + content_height),
    )
    if max(borders) < min_border:
        return None
    return first_column, first_row, content_width, content_height


def compute_diff_bbox(
    width: int,
    height: int,
    previous_rgb: bytes | bytearray,
    current_rgb: bytes | bytearray,
    noise_threshold: int = 12,
) -> dict[str, Any]:
    """Compares two same-sized RGB buffers pixel by pixel.

    A pixel counts as changed when any channel moved by more than
    ``noise_threshold``, which keeps UVC sensor noise from turning every
    capture into a full-screen difference. Rows are skipped whole when their
    bytes match, so a small on-screen change costs a fraction of the frame.
    """
    stride = width * 3
    if len(previous_rgb) < stride * height or len(current_rgb) < stride * height:
        raise ValueError("both RGB buffers must cover the image dimensions")
    total = width * height
    changed = 0
    min_x = min_y = total
    max_x = max_y = -1
    for row in range(height):
        start = row * stride
        previous_row = previous_rgb[start:start + stride]
        current_row = current_rgb[start:start + stride]
        if previous_row == current_row:
            continue
        for column in range(width):
            offset = column * 3
            if (
                abs(previous_row[offset] - current_row[offset]) > noise_threshold
                or abs(previous_row[offset + 1] - current_row[offset + 1]) > noise_threshold
                or abs(previous_row[offset + 2] - current_row[offset + 2]) > noise_threshold
            ):
                changed += 1
                if column < min_x:
                    min_x = column
                if column > max_x:
                    max_x = column
                if row < min_y:
                    min_y = row
                max_y = row
    if changed == 0:
        return {
            "changed": False,
            "bbox_px": None,
            "changed_pixels": 0,
            "diff_ratio": 0.0,
        }
    return {
        "changed": True,
        "bbox_px": {
            "x": min_x,
            "y": min_y,
            "width": max_x - min_x + 1,
            "height": max_y - min_y + 1,
        },
        "changed_pixels": changed,
        "diff_ratio": round(changed / total, 6),
    }
