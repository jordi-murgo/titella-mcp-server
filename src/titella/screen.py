"""Authorized screen capture through the local UVC capture card (ffmpeg)."""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import Config
from .png_codec import (
    compute_diff_bbox,
    crop_rgb,
    decode_png,
    detect_black_borders,
    encode_png_rgb,
)
from .serial_link import TransportError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SCREEN_TIMEOUT_S = 30


def _failure(code: str, message: str, *, retryable: bool) -> dict[str, object]:
    return {
        "ok": False,
        "status": "error",
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def png_resolution(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width > 1 and height > 1 else None


class ScreenCapture:
    def __init__(self, config: Config, window_link: Any | None = None) -> None:
        self.config = config
        # A window link (transport="window") replaces the UVC grabber: the
        # capture is one PNG of the configured macOS window instead of a
        # frame from the local capture card.
        self.window_link = window_link
        state_dir = config.ensure_state_dir()
        self.resolution_path = state_dir / ".capture_res.json"
        self.last_frame_path = state_dir / ".last_frame.png"
        # The frame that preceded the current one, kept for the diff and OCR
        # crops, plus the diff computed between the two.
        self.prev_frame_path = state_dir / ".prev_frame.png"
        self.diff_state_path = state_dir / ".last_diff.json"
        # Source of the last successful capture ("agent" or the UVC device),
        # so measure_diff repeats the same path.
        self.last_source: str | None = None

    def resolution(self) -> tuple[int, int] | None:
        try:
            data = json.loads(self.resolution_path.read_text(encoding="utf-8"))
            width, height = int(data["w"]), int(data["h"])
            return (width, height) if width > 1 and height > 1 else None
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def age_s(self) -> float | None:
        """Age of the last valid capture, in seconds."""
        try:
            return max(0.0, time.time() - self.last_frame_path.stat().st_mtime)
        except OSError:
            return None

    def command(self) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", self.config.screen_input_format,
            "-pixel_format", self.config.screen_pixel_format,
            "-framerate", str(self.config.screen_framerate),
            "-i", self.config.screen_device,
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "png",
            "-",
        ]

    def capture(self) -> tuple[dict, bytes | None]:
        if self.window_link is not None:
            return self._window()
        return self._uvc()

    def agent(self, files) -> tuple[dict, bytes | None]:
        """One on-demand PNG of the running agent's primary monitor."""
        try:
            result = files.screen()
        except TransportError as exc:
            return _failure(exc.code, exc.message, retryable=exc.retryable), None
        image = result.pop("_image")
        resolution = result.pop("_resolution")
        monitor = result.get("monitor")
        if monitor != "primary":
            return _failure(
                "invalid_screen_capture",
                f"fs.screen reported monitor={monitor!r} instead of 'primary'",
                retryable=False,
            ), None
        metadata, image = self.record(
            image, source="agent", resolution=resolution, monitor="primary",
        )
        if metadata.get("ok") and result.get("backend"):
            # Which backend produced this frame (macos-screencapture, x11-import,
            # wayland-portal, windows-dotnet). It is diagnostic only: it never
            # changes how the image or the calibration is treated.
            metadata["backend"] = str(result["backend"])
        return metadata, image

    def record(
        self,
        image: bytes,
        *,
        source: str,
        resolution: tuple[int, int] | None = None,
        monitor: str | None = None,
        raw_resolution: tuple[int, int] | None = None,
        crop_box: dict[str, int] | None = None,
        decoded: tuple[int, int, bytes] | None = None,
    ) -> tuple[dict, bytes | None]:
        """Validates one PNG, persists it and diffs it against the previous frame.

        The received dimensions are authoritative: they are read from the PNG
        itself, so a lying metadata field can never misalign mouse_abs. The
        previous frame is preserved before the new one replaces it, and the
        diff between the two is persisted with the capture so ``screen_diff``
        and ``screen_ocr(scope="diff")`` never have to re-derive it.
        ``decoded`` carries a pre-decoded RGB buffer of ``image`` when the
        caller already had to decode it (the UVC auto-crop path).
        """
        resolution = resolution or png_resolution(image)
        if resolution is None or png_resolution(image) != resolution:
            return _failure(
                "invalid_screen_capture",
                f"the {source} capture is not a valid PNG",
                retryable=True,
            ), None

        previous_bytes: bytes | None = None
        if self.last_frame_path.is_file():
            try:
                previous_bytes = self.last_frame_path.read_bytes()
            except OSError:
                previous_bytes = None
        frame_sha256 = hashlib.sha256(image).hexdigest()
        unchanged = previous_bytes is not None and previous_bytes == image
        diff_info = self._diff_against_previous(
            previous_bytes, image, resolution, unchanged, decoded
        )

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.config.state_dir,
                prefix=".frame-",
                suffix=".png",
                delete=False,
            ) as temporary:
                temporary.write(image)
                temporary_path = Path(temporary.name)
            if previous_bytes is not None:
                self._persist_previous_frame(previous_bytes)
            os.replace(temporary_path, self.last_frame_path)
            resolution_state: dict[str, Any] = {"w": resolution[0], "h": resolution[1]}
            # The raw frame size and the crop box let a window link map a HID
            # coordinate back onto the exact window pixel even when autocrop
            # removed borders, and let it scale a Retina capture onto points.
            if raw_resolution is not None:
                resolution_state["raw_w"] = raw_resolution[0]
                resolution_state["raw_h"] = raw_resolution[1]
            if crop_box is not None:
                resolution_state["crop_box"] = dict(crop_box)
            self.resolution_path.write_text(
                json.dumps(resolution_state), encoding="utf-8"
            )
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            return _failure(
                "screen_state_failed",
                f"the capture cannot be saved: {exc}",
                retryable=False,
            ), None
        metadata: dict[str, object] = {
            "ok": True,
            "path": str(self.last_frame_path),
            "source": source,
            "capture_resolution": {"width_px": resolution[0], "height_px": resolution[1]},
            "frame_sha256": frame_sha256,
            "unchanged_since_previous": unchanged,
            "diff": diff_info,
        }
        if raw_resolution is not None:
            metadata["raw_resolution"] = {
                "width_px": raw_resolution[0],
                "height_px": raw_resolution[1],
            }
        if crop_box is not None:
            metadata["crop_box"] = dict(crop_box)
        if monitor is not None:
            metadata["monitor"] = monitor
        self._persist_diff_state(frame_sha256, unchanged, diff_info, source)
        self.last_source = source
        return metadata, image

    def _persist_previous_frame(self, previous_bytes: bytes) -> None:
        """Keeps the outgoing frame for the next diff; a failure is not fatal."""
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.config.state_dir,
                prefix=".prev-",
                suffix=".png",
                delete=False,
            ) as temporary:
                temporary.write(previous_bytes)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.prev_frame_path)
        except OSError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _diff_against_previous(
        self,
        previous_bytes: bytes | None,
        image: bytes,
        resolution: tuple[int, int],
        unchanged: bool,
        decoded: tuple[int, int, bytes] | None,
    ) -> dict[str, Any]:
        """Diff envelope for the new frame against the stored one."""
        if previous_bytes is None:
            return {
                "changed": False,
                "bbox_px": None,
                "changed_pixels": 0,
                "diff_ratio": 0.0,
                "baseline": True,
            }
        if unchanged:
            return {
                "changed": False,
                "bbox_px": None,
                "changed_pixels": 0,
                "diff_ratio": 0.0,
            }
        width, height = resolution
        full_frame = {
            "changed": True,
            "bbox_px": {"x": 0, "y": 0, "width": width, "height": height},
            "changed_pixels": width * height,
            "diff_ratio": 1.0,
        }
        if png_resolution(previous_bytes) != resolution:
            full_frame["resolution_changed"] = True
            return full_frame
        try:
            previous_width, previous_height, previous_rgb = decode_png(previous_bytes)
            if decoded is not None and decoded[0] == width and decoded[1] == height:
                current_rgb = decoded[2]
            else:
                current_width, current_height, current_rgb = decode_png(image)
                if (current_width, current_height) != (width, height):
                    return full_frame
            if (previous_width, previous_height) != (width, height):
                return full_frame
            return compute_diff_bbox(width, height, previous_rgb, current_rgb)
        except ValueError:
            # A frame that cannot be decoded cannot be diffed; reporting the
            # whole image as changed is honest and keeps the capture usable.
            return full_frame

    def _persist_diff_state(
        self,
        frame_sha256: str,
        unchanged: bool,
        diff_info: dict[str, Any],
        source: str,
    ) -> None:
        state = {
            "frame_sha256": frame_sha256,
            "unchanged_since_previous": unchanged,
            "source": source,
            "diff": diff_info,
        }
        try:
            self.diff_state_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            # The diff state is an accelerator for screen_diff/screen_ocr:
            # losing it costs a recomputation, never the capture itself.
            pass

    def last_diff(self) -> dict[str, Any]:
        """The diff between the last two frames, recomputed when state is lost."""
        try:
            state = json.loads(self.diff_state_path.read_text(encoding="utf-8"))
            diff = state.get("diff") if isinstance(state, dict) else None
            if isinstance(diff, dict):
                return diff
        except (OSError, json.JSONDecodeError):
            pass
        if not (self.prev_frame_path.is_file() and self.last_frame_path.is_file()):
            return {
                "changed": False,
                "bbox_px": None,
                "changed_pixels": 0,
                "diff_ratio": 0.0,
                "baseline": True,
            }
        try:
            previous_bytes = self.prev_frame_path.read_bytes()
            current_bytes = self.last_frame_path.read_bytes()
            current_resolution = png_resolution(current_bytes)
            if previous_bytes == current_bytes:
                return {
                    "changed": False,
                    "bbox_px": None,
                    "changed_pixels": 0,
                    "diff_ratio": 0.0,
                }
            if current_resolution is None or png_resolution(previous_bytes) != current_resolution:
                width, height = current_resolution or (0, 0)
                return {
                    "changed": True,
                    "bbox_px": {"x": 0, "y": 0, "width": width, "height": height},
                    "changed_pixels": width * height,
                    "diff_ratio": 1.0,
                    "resolution_changed": True,
                }
            width, height, previous_rgb = decode_png(previous_bytes)
            _, _, current_rgb = decode_png(current_bytes)
            return compute_diff_bbox(width, height, previous_rgb, current_rgb)
        except (OSError, ValueError):
            return {
                "changed": False,
                "bbox_px": None,
                "changed_pixels": 0,
                "diff_ratio": 0.0,
                "baseline": True,
            }

    def screen_diff(self, padding_px: int = 16) -> tuple[dict, bytes | None]:
        """Crops the last frame to the changed region (plus padding).

        Returns ``(metadata, png)``; without a change there is no image and
        the metadata says ``changed: false``. The crop is clamped to the
        frame bounds, so a bbox at an edge is never padded past the image.
        """
        if not self.last_frame_path.is_file():
            return _failure(
                "screen_required",
                "run screen first to capture a baseline frame",
                retryable=False,
            ), None
        diff = self.last_diff()
        bbox = diff.get("bbox_px")
        if not diff.get("changed") or not isinstance(bbox, dict):
            return {"ok": True, "changed": False, "bbox_px": None, "diff": diff}, None
        try:
            width, height, rgb = decode_png(self.last_frame_path.read_bytes())
        except (OSError, ValueError) as exc:
            return _failure(
                "screen_state_failed",
                f"the last frame cannot be decoded for the diff crop: {exc}",
                retryable=False,
            ), None
        padding = max(0, int(padding_px))
        left = max(0, int(bbox["x"]) - padding)
        top = max(0, int(bbox["y"]) - padding)
        right = min(width, int(bbox["x"]) + int(bbox["width"]) + padding)
        bottom = min(height, int(bbox["y"]) + int(bbox["height"]) + padding)
        if right <= left or bottom <= top:
            return _failure(
                "invalid_screen_capture",
                "the stored diff box has no area inside the last frame",
                retryable=False,
            ), None
        cropped = crop_rgb(width, height, rgb, left, top, right - left, bottom - top)
        return {
            "ok": True,
            "changed": True,
            "bbox_px": {"x": left, "y": top, "width": right - left, "height": bottom - top},
            "raw_bbox_px": dict(bbox),
            "full_resolution": {"width_px": width, "height_px": height},
            "diff": diff,
        }, encode_png_rgb(right - left, bottom - top, cropped)

    def _window(self) -> tuple[dict, bytes | None]:
        """One PNG of the configured macOS window, with the UVC autocrop rules."""
        link = self.window_link
        scratch = self.config.state_dir / "window-capture.png"
        scratch.unlink(missing_ok=True)
        try:
            window, failure = link.capture_window_png(scratch)
        except TransportError as exc:
            scratch.unlink(missing_ok=True)
            return _failure(exc.code, exc.message, retryable=exc.retryable), None
        if failure is not None:
            error = failure.get("error") if isinstance(failure, dict) else None
            if not isinstance(error, dict):
                error = {"code": "screen_capture_failed", "message": "the window capture failed", "retryable": True}
            scratch.unlink(missing_ok=True)
            return _failure(
                str(error.get("code", "screen_capture_failed")),
                str(error.get("message", "the window capture failed")),
                retryable=bool(error.get("retryable", True)),
            ), None
        try:
            image = scratch.read_bytes()
        except OSError as exc:
            return _failure(
                "screen_capture_failed",
                f"the window capture cannot be read: {exc}",
                retryable=True,
            ), None
        finally:
            scratch.unlink(missing_ok=True)
        if png_resolution(image) is None:
            return _failure(
                "invalid_screen_capture",
                "the window capture did not return a valid PNG",
                retryable=True,
            ), None
        raw_resolution: tuple[int, int] | None = None
        crop_box: dict[str, int] | None = None
        decoded: tuple[int, int, bytes] | None = None
        if self.config.screen_autocrop:
            cropped = self._autocrop(image)
            if cropped is not None:
                image, raw_resolution, crop_box, decoded = cropped
            else:
                decoded = self._decode_frame(image)
        window_id = int((window or {}).get("id", 0) or 0)
        return self.record(
            image,
            source=f"window:{window_id}",
            raw_resolution=raw_resolution,
            crop_box=crop_box,
            decoded=decoded,
        )

    def _uvc(self) -> tuple[dict, bytes | None]:
        device = self.config.screen_device
        try:
            completed = subprocess.run(
                self.command(),
                capture_output=True,
                timeout=SCREEN_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _failure(
                "screen_capture_failed",
                f"UVC capture '{device}' failed: {exc}",
                retryable=True,
            ), None
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            return _failure(
                "screen_capture_failed",
                f"UVC capture '{device}' failed ({completed.returncode}): {error[:2000]}",
                retryable=True,
            ), None
        image = completed.stdout
        if png_resolution(image) is None:
            return _failure(
                "invalid_screen_capture",
                f"the capture card '{device}' did not return a valid PNG",
                retryable=True,
            ), None
        raw_resolution: tuple[int, int] | None = None
        crop_box: dict[str, int] | None = None
        decoded: tuple[int, int, bytes] | None = None
        if self.config.screen_autocrop:
            cropped = self._autocrop(image)
            if cropped is not None:
                image, raw_resolution, crop_box, decoded = cropped
            else:
                decoded = self._decode_frame(image)
        return self.record(
            image,
            source=device,
            raw_resolution=raw_resolution,
            crop_box=crop_box,
            decoded=decoded,
        )

    def _decode_frame(self, image: bytes) -> tuple[int, int, bytes] | None:
        try:
            return decode_png(image)
        except ValueError:
            return None

    def _autocrop(
        self, image: bytes
    ) -> tuple[bytes, tuple[int, int], dict[str, int], tuple[int, int, bytes]] | None:
        """Removes pillarbox/letterbox borders from a UVC frame.

        Returns the re-encoded cropped PNG with its metadata, or ``None``
        when the frame has no crop (no black border, or an all-black frame
        that must stay visible as-is).
        """
        decoded = self._decode_frame(image)
        if decoded is None:
            return None
        width, height, rgb = decoded
        box = detect_black_borders(width, height, rgb)
        if box is None:
            return None
        x, y, crop_width, crop_height = box
        cropped_rgb = crop_rgb(width, height, rgb, x, y, crop_width, crop_height)
        cropped = encode_png_rgb(crop_width, crop_height, cropped_rgb)
        return (
            cropped,
            (width, height),
            {"x": x, "y": y, "width": crop_width, "height": crop_height},
            (crop_width, crop_height, cropped_rgb),
        )
