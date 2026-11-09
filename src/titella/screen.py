"""Authorized screen capture through the local UVC capture card (ffmpeg)."""
from __future__ import annotations

import json
import os
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from .config import Config

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
    def __init__(self, config: Config) -> None:
        self.config = config
        state_dir = config.ensure_state_dir()
        self.resolution_path = state_dir / ".capture_res.json"
        self.last_frame_path = state_dir / ".last_frame.png"

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
        resolution = png_resolution(image)
        if resolution is None:
            return _failure(
                "invalid_screen_capture",
                f"the capture card '{device}' did not return a valid PNG",
                retryable=True,
            ), None

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
            os.replace(temporary_path, self.last_frame_path)
            self.resolution_path.write_text(json.dumps({"w": resolution[0], "h": resolution[1]}), encoding="utf-8")
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            return _failure(
                "screen_state_failed",
                f"the capture cannot be saved: {exc}",
                retryable=False,
            ), None
        return {
            "ok": True,
            "path": str(self.last_frame_path),
            "source": device,
            "capture_resolution": {"width_px": resolution[0], "height_px": resolution[1]},
        }, image
