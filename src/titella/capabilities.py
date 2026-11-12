"""Slave channel capability discovery and slave-variant selection.

The window transport can reach a remote machine through several channels and
they are not equivalent: a Citrix seamless session forwards pointer input but
drops the keyboard, an X11 client such as XQuartz/FreeRDP forwards keys but
mangles the characters that are not on the slave layout, and a remote-desktop
clipboard stops relaying once the guest has taken ownership of it. Picking an
agent protocol by guesswork is what turns a working session into an
unexplained timeout, so the protocol starts by measuring.

``PROBE_SCRIPT`` runs on the slave (over the agent channel once one exists, or
typed into a console before that) and reports what the machine itself offers:
cameras (video in), capture audio endpoints, and whether its own clipboard API
can round-trip text and images. ``parse_caps()`` reads the marker line back and
``recommend_variant()`` turns that into the agent protocol to use, with the
reason for the choice recorded next to it.

Capability directions are named from the master's point of view, matching the
rest of the codebase: ``m2s`` is master to slave, ``s2m`` is slave to master.
"""
from __future__ import annotations

import json
import re
from typing import Any

# One line, no commas: a comma is the character a non-US layout most often
# mangles when the probe is typed rather than sent over an agent channel.
CAPS_MARKER = "TITELLA_CAPS:"

# How the script must reach the slave. It contains square brackets and type
# accelerators (`[Windows.Forms.Clipboard]`, `[System.Drawing.Color]`), and an
# X11 client such as XQuartz/FreeRDP does not forward those keystrokes: `[`
# arrives as a backtick and `]` as a plus, so a typed copy fails with a
# ParserError. Ship it over the clipboard and run it with
# `iex (Get-Clipboard -Raw)`; only that launch line is typed.
PROBE_DELIVERY = "clipboard"

PAYLOAD_MARKER = "TITELLA_PAYLOAD:"


def payload_probe_script(size: int) -> str:
    """One line that reports how many characters of a known payload arrived.

    The master writes ``PAYLOAD_PROBE`` followed by ``size`` identical letters
    and the slave answers with the length it actually holds, so a bridge that
    truncates is caught by comparison instead of by a mysterious timeout.
    """
    if size < 16:
        raise ValueError("the probe payload must be at least 16 characters")
    return (
        "$c=Get-Clipboard -Raw; "
        "Write-Output ('" + PAYLOAD_MARKER + "'+$c.Length+'/'+$c.Substring(0,13))"
    )


def payload_probe_text(size: int) -> str:
    """The payload the master puts on the pasteboard for the probe."""
    prefix = "PAYLOAD_PROBE"
    if size <= len(prefix):
        raise ValueError("the probe payload must be at least 16 characters")
    return prefix + "X" * (size - len(prefix))


def parse_payload_probe(output: str) -> dict[str, Any]:
    """Reads the slave's answer: how many characters arrived, and its head."""
    match = re.search(re.escape(PAYLOAD_MARKER) + r"(\d+)/(\S*)", output)
    if not match:
        raise ValueError("no TITELLA_PAYLOAD marker in the output")
    return {"received": int(match.group(1)), "head": match.group(2)}


def measure_ceiling(received: int, sent: int) -> int:
    """The ceiling the measurement proves: intact means the sent size."""
    if received < 0 or sent <= 0:
        raise ValueError("invalid payload measurement")
    return sent if received >= sent else received


PROBE_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "$cam=@(Get-PnpDevice -PresentOnly -Class Camera | % FriendlyName) "
    "+ @(Get-PnpDevice -PresentOnly -Class Image | % FriendlyName); "
    "$aud=@(Get-PnpDevice -PresentOnly -Class AudioEndpoint | % FriendlyName); "
    "$media=@(Get-PnpDevice -PresentOnly -Class MEDIA | % FriendlyName); "
    "$t='TITELLA_CAPS_TEXT'; "
    "Set-Clipboard -Value $t; $textOk=((Get-Clipboard) -eq $t); "
    "$imgOk=$false; "
    "try { "
    "$b=New-Object System.Drawing.Bitmap 8 8; "
    "$b.SetPixel(0 0 [System.Drawing.Color]::Red); "
    "[System.Windows.Forms.Clipboard]::SetImage($b); "
    "$r=[System.Windows.Forms.Clipboard]::GetImage(); "
    "$imgOk=($null -ne $r -and $r.Width -eq 8 -and $r.Height -eq 8); "
    "} catch { $imgOk=$false }; "
    "$o=@{cameras=$cam; audio_endpoints=$aud; media=$media; "
    "clipboard_text=$textOk; clipboard_image=$imgOk}; "
    "Write-Output ('" + CAPS_MARKER + "' + ($o | ConvertTo-Json -Compress))"
)

_MARKER_RE = re.compile(re.escape(CAPS_MARKER) + r"(\{.*\})")

# The clipboard bridge of a remote-desktop client may forward only the first
# N characters of a large text payload and silently drop the rest: measured at
# 158 characters through xfreerdp 3.33.1 inside XQuartz, while small payloads
# arrive complete. The ceiling therefore has to be measured per session before
# a bootstrap method is chosen, because it decides how many chunks the loader
# needs — and a method that cannot carry the loader is not a method.
PAYLOAD_PROBE_SIZES = (64, 512, 4096)

# The clipboard API also needs a single-threaded apartment, and the Windows
# agent runs its shell on an MTA runspace: `Set-Clipboard` then fails with
# "Current thread must be set to single thread apartment (STA) mode". That makes
# the clipboard a launcher channel only — it can start an agent but cannot serve
# one — which is why the capability map records it as unavailable on this side.
CLIPBOARD_AGENT_THREAD = "mta"


def parse_caps(text: str) -> dict[str, Any]:
    """Extracts the capability object from console or shell output."""
    match = _MARKER_RE.search(text)
    if not match:
        raise ValueError("no TITELLA_CAPS marker in the output")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed capability payload: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("the capability payload is not an object")
    cameras = data.get("cameras") or []
    audio = data.get("audio_endpoints") or []
    media = data.get("media") or []
    if isinstance(cameras, str):
        cameras = [cameras]
    if isinstance(audio, str):
        audio = [audio]
    if isinstance(media, str):
        media = [media]
    caps: dict[str, Any] = {
        "video_in": {
            "available": bool(cameras),
            "devices": [str(item) for item in cameras],
        },
        "audio_in": {
            "available": bool(audio),
            "devices": [str(item) for item in audio],
            "media": [str(item) for item in media],
        },
        "clipboard_text": {
            # The slave-side API working says nothing about the transport, so
            # the direction flags stay unknown until the master probes them.
            "local_api": bool(data.get("clipboard_text")),
            "m2s": None,
            "s2m": None,
        },
        "clipboard_image": {
            "local_api": bool(data.get("clipboard_image")),
            "m2s": None,
            "s2m": None,
        },
    }
    return caps


def apply_transport_facts(
    caps: dict[str, Any],
    *,
    text_m2s: bool | None = None,
    text_s2m: bool | None = None,
    image_m2s: bool | None = None,
    image_s2m: bool | None = None,
    payload_ceiling: int | None = None,
) -> dict[str, Any]:
    """Folds master-side transport observations into a capability map.

    The slave probe measures devices and its own clipboard API; only the
    master can observe what survives the transport. Both halves are needed
    before the variant can be chosen, so the directions start unknown.

    ``payload_ceiling`` is the longest text the transport actually delivered
    intact. It is measured, never assumed: a bridge that truncates silently
    turns a working protocol into a stream of ParserErrors.
    """
    merged = json.loads(json.dumps(caps))
    merged["clipboard_text"]["m2s"] = text_m2s
    merged["clipboard_text"]["s2m"] = text_s2m
    merged["clipboard_text"]["payload_ceiling"] = payload_ceiling
    merged["clipboard_image"]["m2s"] = image_m2s
    merged["clipboard_image"]["s2m"] = image_s2m
    return merged


def chunk_chars_for(caps: dict[str, Any], *, default: int = 2048) -> int:
    """Packet size the measured ceiling allows, with a safety margin.

    The ceiling bounds the whole packet, header included, so a quarter of it is
    reserved for the envelope (prefix, sequence numbers and the base64 body of
    one frame). An unmeasured channel keeps the caller's default: nothing is
    shrunk on a guess.
    """
    ceiling = (caps.get("clipboard_text") or {}).get("payload_ceiling")
    if not isinstance(ceiling, int) or ceiling <= 0:
        return default
    return max(64, ceiling // 4)


def recommend_variant(caps: dict[str, Any]) -> dict[str, Any]:
    """Chooses the slave variant that the measured channels support.

    Ordering is by what each channel can carry, most capable first:

    - ``clipboard``: needs text in both directions for the protocol frames; the
      image direction only speeds up the bulk transfer, so its absence costs
      throughput, never correctness.
    - ``visual``: needs nothing but a capture of the slave screen; the slowest
      and least reliable, so it is the last resort.
    - ``none``: no channel can carry an agent; screen and HID only.

    A direction that was never measured is ``None`` and is never read as
    working: an unproven channel is the one that fails at the worst moment.
    A measured ``payload_ceiling`` is reported too, because it decides how many
    exchanges the loader needs and whether the channel can carry it at all.
    """
    text = caps.get("clipboard_text") or {}
    image = caps.get("clipboard_image") or {}
    ceiling = text.get("payload_ceiling")
    reasons: list[str] = []
    if text.get("m2s") and text.get("s2m"):
        reasons.append("clipboard text works in both directions")
        if isinstance(ceiling, int) and ceiling > 0:
            reasons.append(
                f"the transport delivered at most {ceiling} characters intact: "
                f"packets of {chunk_chars_for(caps)} characters keep the loader within it"
            )
        if image.get("m2s"):
            reasons.append("clipboard images work master to slave: bulk transfer is fast")
        else:
            reasons.append("clipboard images are unavailable: the loader travels as text chunks")
        return {"agent_protocol": "clipboard", "reasons": reasons}
    if image.get("m2s") and not text.get("s2m"):
        reasons.append(
            "clipboard images work master to slave but text does not come back: "
            "no channel can acknowledge packets"
        )
        return {"agent_protocol": "none", "reasons": reasons}
    if text.get("m2s") or text.get("s2m"):
        reasons.append("clipboard text works in one direction only: the protocol needs both")
        return {"agent_protocol": "none", "reasons": reasons}
    reasons.append("no clipboard direction was observed: the agent needs a capture path")
    return {"agent_protocol": "visual", "reasons": reasons}
