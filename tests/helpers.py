"""Small transports shared by the timing/polling tests."""

from __future__ import annotations

import time

from stiebel_heatpump.protocol import parser
from stiebel_heatpump.protocol.transport import SimulatorTransport, TransportError


class RecordingTransport(SimulatorTransport):
    """Simulator that notes when every conversation starts (the 0x02 byte)."""

    def __init__(self, simulator) -> None:
        super().__init__(simulator)
        self.starts: list[float] = []

    def write(self, data: bytes) -> None:
        if data == bytes([parser.START_COMMUNICATION]):
            self.starts.append(time.monotonic())
        super().write(data)


class FlakyTransport(RecordingTransport):
    """Simulator whose link can be switched off, like an unplugged adapter."""

    broken = False

    def open(self) -> None:
        if self.broken:
            raise TransportError("link is down")

    def write(self, data: bytes) -> None:
        if self.broken:
            raise TransportError("link is down")
        super().write(data)


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
