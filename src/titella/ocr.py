"""Configurable OCR through an OpenAI-compatible multimodal endpoint.

The model is chosen in configuration (``ocr_endpoint``, ``ocr_api_key``,
``ocr_model``), never hard-coded, and only local-inference endpoints are
meant to be configured. The image sent is a PNG region of the last capture:
the diff box by default, the whole frame on request, or a caller-provided
rectangle.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

from .config import Config
from .png_codec import crop_rgb, decode_png, encode_png_rgb
from .screen import ScreenCapture, png_resolution

DEFAULT_OCR_PROMPT = (
    "Extract every piece of readable text from this screenshot region. "
    "Transcribe it verbatim, preserving line breaks and the original "
    "language. Do not translate, explain or wrap the text."
)
# The diff scope is padded so glyphs clipped by the changed box stay whole.
DIFF_PADDING_PX = 16


def _failure(code: str, message: str, *, retryable: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def _endpoint_url(endpoint: str) -> str:
    trimmed = endpoint.rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    return f"{trimmed}/chat/completions"


def _content_text(content: Any) -> str:
    """Normalizes ``choices[0].message.content`` to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def _crop_frame(
    image: bytes, bbox: dict[str, int], padding: int
) -> tuple[dict[str, int], bytes]:
    """Crops a padded, clamped rectangle of an RGB PNG."""
    width, height, rgb = decode_png(image)
    padding = max(0, int(padding))
    left = max(0, int(bbox["x"]) - padding)
    top = max(0, int(bbox["y"]) - padding)
    right = min(width, int(bbox["x"]) + int(bbox["width"]) + padding)
    bottom = min(height, int(bbox["y"]) + int(bbox["height"]) + padding)
    if right <= left or bottom <= top:
        raise ValueError("the OCR crop has no area inside the frame")
    used = {"x": left, "y": top, "width": right - left, "height": bottom - top}
    return used, encode_png_rgb(right - left, bottom - top, crop_rgb(width, height, rgb, left, top, right - left, bottom - top))


def _validated_bbox(bbox: Any, image: bytes) -> tuple[dict[str, int] | None, dict[str, Any] | None]:
    """Validates a caller-provided bbox against the frame dimensions."""
    if not isinstance(bbox, dict):
        return None, _failure(
            "invalid_argument",
            "scope='bbox' requires bbox={'x','y','width','height'}",
            retryable=False,
        )
    try:
        box = {key: int(bbox[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None, _failure(
            "invalid_argument",
            "bbox requires integer x, y, width and height",
            retryable=False,
        )
    resolution = png_resolution(image)
    if resolution is None:
        return None, _failure(
            "invalid_screen_capture",
            "the last frame is not a valid PNG",
            retryable=False,
        )
    width, height = resolution
    if box["width"] < 1 or box["height"] < 1:
        return None, _failure(
            "invalid_argument", "bbox width and height must be positive", retryable=False
        )
    if (
        box["x"] < 0
        or box["y"] < 0
        or box["x"] + box["width"] > width
        or box["y"] + box["height"] > height
    ):
        return None, _failure(
            "coordinates_out_of_bounds",
            f"bbox exceeds the {width}x{height} frame",
            retryable=False,
        )
    return box, None


def run_ocr(
    config: Config,
    capture: ScreenCapture,
    *,
    scope: str = "diff",
    bbox: dict[str, int] | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Sends one PNG region of the last capture to the configured OCR model.

    ``scope`` selects the region: ``diff`` (the changed box of the last two
    captures, falling back to the full frame when there is no baseline),
    ``full`` (the whole last frame) or ``bbox`` (a caller-provided
    rectangle). An unchanged diff answers without calling the model at all.
    """
    if not config.ocr_endpoint or not config.ocr_model:
        return _failure(
            "ocr_not_configured",
            "configure ocr_endpoint and ocr_model in config.toml or "
            "TITELLA_OCR_ENDPOINT / TITELLA_OCR_MODEL",
            retryable=False,
        )
    if scope not in {"diff", "full", "bbox"}:
        return _failure(
            "invalid_argument", "scope must be diff, full or bbox", retryable=False
        )
    if not capture.last_frame_path.is_file():
        return _failure(
            "screen_required",
            "run screen first before running screen_ocr",
            retryable=False,
        )
    try:
        image = capture.last_frame_path.read_bytes()
    except OSError as exc:
        return _failure(
            "screen_state_failed",
            f"the last frame cannot be read: {exc}",
            retryable=True,
        )

    effective_scope = scope
    used_bbox: dict[str, int] | None = None
    if scope == "diff":
        diff = capture.last_diff()
        if not diff.get("baseline") and not diff.get("changed"):
            # Nothing changed since the previous capture: there is nothing
            # new to read, and guessing from the whole frame would answer a
            # question the caller did not ask.
            return {
                "ok": True,
                "changed": False,
                "text": "",
                "scope": "diff",
                "bbox_px": None,
            }
        box = diff.get("bbox_px") if diff.get("changed") else None
        if not isinstance(box, dict):
            # No baseline frame yet: the only honest region is the whole frame.
            effective_scope = "full"
        else:
            try:
                used_bbox, image = _crop_frame(image, box, DIFF_PADDING_PX)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                return _failure(
                    "invalid_screen_capture",
                    f"the diff box cannot be cropped from the last frame: {exc}",
                    retryable=False,
                )
    elif scope == "bbox":
        box, error = _validated_bbox(bbox, image)
        if error is not None:
            return error
        assert box is not None  # a validated bbox is never None
        try:
            used_bbox, image = _crop_frame(image, box, 0)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            return _failure(
                "invalid_screen_capture",
                f"the requested bbox cannot be cropped from the last frame: {exc}",
                retryable=False,
            )

    payload = {
        "model": config.ocr_model,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or DEFAULT_OCR_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(image).decode("ascii")
                        },
                    },
                ],
            }
        ],
    }
    headers = {"Content-Type": "application/json"}
    if config.ocr_api_key:
        headers["Authorization"] = f"Bearer {config.ocr_api_key}"
    request = urllib.request.Request(
        _endpoint_url(config.ocr_endpoint),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.ocr_timeout_s) as response:
            body = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return _failure(
            "ocr_request_failed",
            f"the OCR endpoint request failed: {exc}",
            retryable=True,
        )
    try:
        document = json.loads(body.decode("utf-8"))
        content = document["choices"][0]["message"]["content"]
        text = _content_text(content)
    except (ValueError, KeyError, IndexError, TypeError, UnicodeDecodeError) as exc:
        return _failure(
            "ocr_request_failed",
            f"the OCR endpoint returned an unusable chat completion: {exc}",
            retryable=True,
        )
    return {
        "ok": True,
        "text": text,
        "scope": effective_scope,
        "bbox_px": used_bbox,
        "model": config.ocr_model,
    }
