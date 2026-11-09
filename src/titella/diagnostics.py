"""Aggregated KVM status and actionable next steps.

It replaces the external documentation that used to live in skills: the answer
comes from the real state of the daemon, the firmware, the lease, the agent and
the last capture, not from static prose.
"""
from __future__ import annotations

from typing import Any

from .config import Config

# A capture older than this no longer serves as feedback from the slave.
STALE_CAPTURE_S = 120


def _capture_state(config: Config, resolution: tuple[int, int] | None, age_s: float | None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "source": config.screen_device,
        "input_format": config.screen_input_format,
        "pixel_format": config.screen_pixel_format,
        "framerate": config.screen_framerate,
        "resolution": list(resolution) if resolution else None,
        "last_frame_age_s": round(age_s, 1) if age_s is not None else None,
    }
    return state


def _agent_step(config: Config, agent: dict[str, Any]) -> str:
    """Bootstrap guidance for the selected platform, never Linux-only advice."""
    platform = agent.get("platform") or config.slave_platform
    if platform == "windows":
        return (
            "The Windows slave agent is not running: verify the active input source of the "
            "focused slave window with screen(), then slave_bootstrap(owner, platform=\"windows\", "
            "layout=..., layout_confirmed=true) deploys the C# payload over HID + CDC. A missing "
            "or unconfirmed layout answers slave_layout_required or slave_layout_unconfirmed "
            "before any keystroke; a layout that cannot be proven for the console the launcher "
            "will open is a reason to stop, not to confirm. If it fails with "
            "bootstrap_loader_timeout the loader console could not open the COM port or a policy "
            "blocked it; pass slave_cdc_port when Windows cannot identify the Titella port "
            "unambiguously, and check that interactive PowerShell 5.1 with FullLanguage is "
            "allowed."
        )
    if platform == "linux":
        return (
            "The slave agent is not running: verify the active input source of the focused "
            "slave window with screen(), then slave_bootstrap(owner, platform=\"linux\", "
            "layout=..., layout_confirmed=true) deploys the zipapp over HID + CDC. A missing or "
            "unconfirmed layout answers slave_layout_required or slave_layout_unconfirmed before "
            "any keystroke. If it fails with bootstrap_loader_timeout it is usually a CDC "
            "permission problem (slave user not in the uucp group) or focus on the wrong window, "
            "not the protocol."
        )
    if platform == "macos":
        return (
            "The slave agent is not running: verify the active input source of the focused "
            "slave window with screen(), then slave_bootstrap(owner, platform=\"macos\", "
            "layout=..., layout_confirmed=true) deploys the zipapp over HID + CDC. A missing or "
            "unconfirmed layout answers slave_layout_required or slave_layout_unconfirmed before "
            "any keystroke. If it fails with bootstrap_loader_timeout it is usually focus on a "
            "window that is not the terminal or a CDC permission problem on the slave Mac."
        )
    return (
        "The slave platform is not selected: slave_bootstrap(owner, platform=..., layout=..., "
        "layout_confirmed=true) takes macos, linux or windows and deploys the matching agent "
        "over HID + CDC. The platform is never inferred from the master, and the layout is "
        "never taken from configuration: verify the target's active input source first, or the "
        "call answers slave_layout_required."
    )


def next_steps(
    config: Config,
    *,
    firmware: dict[str, Any],
    lease: dict[str, Any],
    agent: dict[str, Any],
    resolution: tuple[int, int] | None,
    capture_age_s: float | None,
) -> list[str]:
    """Pending steps, in the order in which they must be resolved."""
    steps: list[str] = []
    if not firmware.get("reachable"):
        steps.append(
            "The ESP32 does not answer protocol v1: check TITELLA_TRANSPORT and the port "
            f"({config.transport}). A chip in reset emits no banner; a firmware older than "
            "envelope v1 replies without `v`."
        )
    if not lease.get("locked"):
        steps.append(
            "No client holds the HID lease: call lock_acquire(owner) before "
            "mouse_abs, mouse_rel, click, wheel, key, type or slave_bootstrap."
        )
    if agent.get("status") != "ready":
        steps.append(_agent_step(config, agent))
    if resolution is None:
        steps.append(
            "There is no previous capture: run screen() first; mouse_abs calibrates against the "
            "captured resolution and returns screen_required without it."
        )
    elif capture_age_s is not None and capture_age_s > STALE_CAPTURE_S:
        steps.append(
            f"The last capture is {int(capture_age_s)} s old: run screen() again before trusting "
            "the cursor position or the screen state."
        )
    if not steps:
        steps.append("Everything is ready: lease, agent and capture are current.")
    return steps


def diagnose(
    config: Config,
    *,
    firmware: dict[str, Any],
    lease: dict[str, Any],
    agent: dict[str, Any],
    resolution: tuple[int, int] | None,
    capture_age_s: float | None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "firmware": firmware,
        "lease": lease,
        "agent": agent,
        "capture": _capture_state(config, resolution, capture_age_s),
        "next_steps": next_steps(
            config,
            firmware=firmware,
            lease=lease,
            agent=agent,
            resolution=resolution,
            capture_age_s=capture_age_s,
        ),
    }
