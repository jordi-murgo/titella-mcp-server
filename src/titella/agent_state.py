"""Shared availability state of the slave agent."""
from __future__ import annotations

import threading

from .serial_link import TransportError


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
