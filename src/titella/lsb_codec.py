"""LSB image steganography channel for the clipboard transport.

``encode_payload_png()`` renders a standalone PNG whose blue channel LSBs
carry arbitrary binary bytes; ``decode_payload_png()`` is the reference
decoder used by the master to round-trip and by tests. The remote side uses
the PowerShell decoder embedded in the clipboard stub, which mirrors this
specification exactly.

Format (version 1)
- Image: 3 bytes per pixel (RGB), 8 blue bytes plus 2 header bytes of
  capacity per pixel row... see HEADER for the precise layout; the format is
  designed so one byte is carried by one pixel's blue LSB, with a fixed
  16-byte header at the start of the byte stream.
- Byte stream = HEADER magic + version + payload_len + reserved + payload.
  bytes 0..3   magic "TLSB"
  byte  4      format version (1)
  bytes 5..6   payload_len (big-endian)
  byte  7      reserved (0)
  bytes 8..15  CRC-32 (zlib) of payload
  bytes 16..   payload
- The byte stream maps to pixels row-major: byte i lives in pixel i, blue
  channel LSB. Red and green carry a fixed checkerboard pattern so the
  image is not a solid block and decoders can sanity-check alignment via
  the magic.
- The decoder reads only the blue LSBs, revalidates magic, version, length
  and CRC, and returns the payload bytes. It never guesses: any mismatch
  is an error.

Channel choice: the blue channel only (1 bit per pixel) rather than all
three channels (3 bits per pixel). Measured overhead for a given payload:
1 bit/px costs 8 pixels per byte (8x expansion); 3 bit/px costs 8/3 pixels
per byte (2.67x). The clipboard path re-encodes the image through Citrix/
RDP sync where PNGs are transferred losslessly, so capacity per image is
not the bottleneck; robustness to any alpha or palette mangling is.
Using blue-only keeps the PowerShell decoder trivial (one channel scan)
and leaves red/green as sync pattern for visual debugging.
"""

from __future__ import annotations

import zlib

from .png_codec import decode_png, encode_png_rgb

MAGIC = b"TLSB"
FORMAT_VERSION = 1
# bytes 0..3 magic, 4 version, 5..6 payload length, 7 reserved, 8..11 CRC-32.
HEADER_LEN = 12
# Red/green checkerboard so the image is visibly patterned and a decoder
# can verify pixel alignment when debugging.
CHECKER_SIZE = 8

def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _stream_for(payload: bytes) -> bytes:
    """HEADER || payload: the byte stream the pixels will carry."""
    if len(payload) > 0xFFFF:
        raise ValueError("payload exceeds 65535 bytes")
    stream = bytearray()
    stream += MAGIC
    stream.append(FORMAT_VERSION)
    stream += len(payload).to_bytes(2, "big")
    stream.append(0)
    stream += _crc32(payload).to_bytes(4, "big")
    stream += payload
    return bytes(stream)


def _bit(stream: bytes, index: int) -> int:
    """Bit g of the stream: byte g//8, bit 7 - g%8 (MSB first)."""
    return (stream[index // 8] >> (7 - index % 8)) & 1


def _bytes_from_bits(bits: str, count: int) -> bytes:
    if len(bits) < count * 8:
        raise ValueError("image too small for the declared payload")
    return bytes(int(bits[i * 8:(i + 1) * 8], 2) for i in range(count))


def _layout(pixel_count: int) -> tuple[int, int]:
    """Image dimensions for a given pixel count."""
    width = 256 if pixel_count <= 256 * 256 else 1024
    height = (pixel_count + width - 1) // width
    return width, height


def encode_payload_png(payload: bytes) -> bytes:
    """Embeds payload bytes in a PNG, one bit per pixel (blue LSB).

    Bit g of the stream lives in pixel g: byte g//8, bit 7 - g%8, so the
    first eight pixels carry the magic and a decoder can verify alignment.

    The carrier is pure white, not a checkerboard: a remote-desktop hop runs
    the image through a colour conversion that snaps dark blues down a level
    (254 arrives as 253) and destroys the bit, while the top of the range is
    preserved exactly. The payload therefore rides only on 254/255.
    """
    stream = _stream_for(payload)
    pixel_count = len(stream) * 8
    width, height = _layout(pixel_count)
    rgb = bytearray(b"\xff" * (width * height * 3))
    for index in range(pixel_count):
        offset = index * 3
        rgb[offset + 2] = 254 | _bit(stream, index)
    return encode_png_rgb(width, height, rgb)


def decode_payload_png(png_bytes: bytes) -> bytes:
    """Reads back the payload bytes written by encode_payload_png."""
    _width, _height, rgb = decode_png(png_bytes)
    pixel_count = len(rgb) // 3
    bits = "".join(str(rgb[i * 3 + 2] & 1) for i in range(min(pixel_count, HEADER_LEN * 8)))
    header = _bytes_from_bits(bits, HEADER_LEN)
    if header[:4] != MAGIC:
        raise ValueError("bad magic: not a TLSB payload image")
    if header[4] != FORMAT_VERSION:
        raise ValueError(f"unsupported format version {header[4]}")
    payload_len = int.from_bytes(header[5:7], "big")
    needed_bits = (HEADER_LEN + payload_len) * 8
    if payload_len == 0:
        raise ValueError("empty payload")
    if needed_bits > pixel_count:
        raise ValueError("payload length exceeds image capacity")
    all_bits = "".join(str(rgb[i * 3 + 2] & 1) for i in range(needed_bits))
    payload = _bytes_from_bits(all_bits[HEADER_LEN * 8:], payload_len)
    if _crc32(payload) != int.from_bytes(header[8:12], "big"):
        raise ValueError("payload CRC mismatch")
    return payload


def encode_payload_png_3channel(payload: bytes) -> bytes:
    """Variant embedding 3 bits per pixel (R, G, B LSBs) for comparison.

    The bit trios follow the stream bits in order: bit 3i+0 of the bit
    string lands in the red LSB of pixel i, bit 3i+1 in green, bit 3i+2 in
    blue. Red and green carry real payload here, so the checkerboard
    visual aid is dropped for this variant.
    """
    stream = _stream_for(payload)
    bits = "".join(f"{byte:08b}" for byte in stream)
    pixel_count = (len(bits) + 2) // 3
    width, height = _layout(pixel_count)
    rgb = bytearray(width * height * 3)
    for index in range(pixel_count):
        offset = index * 3
        x = index % width
        y = index // width
        base = (254 if (((x // CHECKER_SIZE) + (y // CHECKER_SIZE)) % 2) else 254)
        rgb[offset] = base | int(bits[index * 3]) if index * 3 < len(bits) else 254
        rgb[offset + 1] = base | int(bits[index * 3 + 1]) if index * 3 + 1 < len(bits) else 254
        rgb[offset + 2] = base | int(bits[index * 3 + 2]) if index * 3 + 2 < len(bits) else 254
    return encode_png_rgb(width, height, rgb)


def decode_payload_png_3channel(png_bytes: bytes) -> bytes:
    """Reads back a 3-bit-per-pixel payload image."""
    _width, _height, rgb = decode_png(png_bytes)
    pixel_count = len(rgb) // 3
    bits = []
    for index in range(pixel_count):
        offset = index * 3
        bits.append(rgb[offset] & 1)
        bits.append(rgb[offset + 1] & 1)
        bits.append(rgb[offset + 2] & 1)
    bits = "".join(str(b) for b in bits)
    header = _bytes_from_bits(bits, HEADER_LEN)
    if header[:4] != MAGIC:
        raise ValueError("bad magic: not a TLSB payload image")
    if header[4] != FORMAT_VERSION:
        raise ValueError(f"unsupported format version {header[4]}")
    payload_len = int.from_bytes(header[5:7], "big")
    if payload_len == 0:
        raise ValueError("empty payload")
    needed_bits = (HEADER_LEN + payload_len) * 8
    if needed_bits > len(bits):
        raise ValueError("payload length exceeds image capacity")
    payload = _bytes_from_bits(bits[HEADER_LEN * 8:], payload_len)
    if _crc32(payload) != int.from_bytes(header[8:12], "big"):
        raise ValueError("payload CRC mismatch")
    return payload
