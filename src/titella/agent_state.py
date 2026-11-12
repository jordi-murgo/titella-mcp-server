"""Shared availability state of the slave agent."""
from __future__ import annotations

import threading
from typing import Any

from .serial_link import TransportError


def require_cdc_channel(link: Any) -> None:
    """Rejects remote operations on a transport without an agent channel.

    The channel the slave agent speaks over is the ``agent_protocol`` of the
    link: ``serial`` (the CDC device of a physical rig), ``clipboard`` (the
    shared clipboard of the window transport, packetised as M2S/S2M frames) or
    ``none``/``visual`` (not available). Links that predate the field carry
    their channel natively, so a missing attribute never rejects anything.
    """
    protocol = getattr(link, "agent_protocol", None)
    if protocol is None or protocol == "serial":
        return
    if protocol == "clipboard":
        return
    if protocol == "visual":
        raise TransportError(
            "cdc_unavailable",
            "visual agent_protocol is not implemented yet; use agent_protocol='clipboard'",
            False,
        )
    raise TransportError(
        "cdc_unavailable",
        "window transport has no CDC channel with agent_protocol=none; "
        "set agent_protocol='clipboard' to enable the slave agent",
        False,
    )


class AgentGate:
    """Keeps remote calls from waiting on an agent that does not exist."""

    def __init__(self) -> None:
        self._ready = False
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    def set_ready(self, value: bool) -> None:
        with self._lock:
            self._ready = value

    def require(self) -> None:
        with self._lock:
            if self._ready:
                return
        raise TransportError(
            "agent_not_ready",
            "the slave agent is not running; run slave_bootstrap",
            True,
        )
