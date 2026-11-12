"""Runs the slave capability probe and folds in master-side transport facts.

The probe is the first thing the protocol does on a new slave: it measures what
the machine itself offers and, together with the directions the master has
observed on the transport, decides which slave variant the session uses. The
module keeps the device half (``capabilities``) and the measurement plumbing
apart so the parsing stays testable without a slave.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .capabilities import (
    PROBE_SCRIPT,
    apply_transport_facts,
    parse_caps,
    recommend_variant,
)


class CapabilityProber:
    """Measures one slave's channels and records the chosen variant.

    ``run`` is transport-agnostic: it hands the probe script to ``execute``,
    which the caller wires to the running agent's shell or to a console it
    types into. The prober never decides how to reach the slave.
    """

    def __init__(self, *, timeout_s: float = 60.0) -> None:
        self.timeout_s = timeout_s
        self._last: dict[str, Any] | None = None

    def probe_script(self) -> str:
        return PROBE_SCRIPT

    def record(self, raw_output: str, *, transport: dict[str, Any] | None = None) -> dict[str, Any]:
        """Parses one probe answer and returns the capability report."""
        caps = parse_caps(raw_output)
        if transport:
            caps = apply_transport_facts(
                caps,
                text_m2s=transport.get("text_m2s"),
                text_s2m=transport.get("text_s2m"),
                image_m2s=transport.get("image_m2s"),
                image_s2m=transport.get("image_s2m"),
            )
        selection = recommend_variant(caps)
        self._last = {
            "ok": True,
            "channels": caps,
            "selection": selection,
            "measured_at": time.time(),
        }
        return dict(self._last)

    def status(self) -> dict[str, Any]:
        if self._last is None:
            return {
                "ok": True,
                "status": "not_measured",
                "next": (
                    "run the capability probe before choosing an agent protocol: "
                    "it reports video-in and audio-in devices plus the clipboard "
                    "directions the transport actually carries"
                ),
            }
        return dict(self._last)

    def to_json(self) -> str:
        return json.dumps(self.status(), indent=2, sort_keys=True)
