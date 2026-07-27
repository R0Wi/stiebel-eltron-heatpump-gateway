"""Byte transports for talking to the heat pump.

The rest of the protocol stack only depends on the small :class:`Transport`
interface -- ``write`` bytes and ``get`` one byte at a time -- so it works
identically against a real serial port and against the in-memory simulator used
for tests and offline demos.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from ..simulator import HeatPumpSimulator


class NoDataAvailable(Exception):
    """Raised by :meth:`Transport.get` when no byte arrives in time."""


@runtime_checkable
class Transport(Protocol):
    """Minimal byte-oriented transport."""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def write(self, data: bytes) -> None: ...

    def get(self, timeout: float = 1.0) -> int:
        """Return the next received byte (0..255) or raise :class:`NoDataAvailable`."""
        ...


class SerialTransport:
    """A real serial port transport backed by ``pyserial``.

    A background thread continuously reads bytes into a thread-safe queue,
    mirroring the binding's ``ByteStreamReceiver`` + ``CircularByteBuffer``.
    """

    def __init__(self, port: str, baudrate: int = 9600) -> None:
        self._port = port
        self._baudrate = baudrate
        self._serial = None
        self._queue: "queue.Queue[int]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._running = False

    def open(self) -> None:
        import serial  # imported lazily so the package works without hardware deps

        self._serial = serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
        )
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self._serial is not None
        while self._running:
            try:
                chunk = self._serial.read(1)
                if chunk:
                    self._queue.put(chunk[0])
                else:
                    time.sleep(0.003)
            except Exception:  # noqa: BLE001 - keep the reader alive
                time.sleep(0.03)

    def close(self) -> None:
        self._running = False
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def write(self, data: bytes) -> None:
        if self._serial is None:
            raise RuntimeError("serial port is not open")
        self._serial.write(data)
        self._serial.flush()

    def get(self, timeout: float = 1.0) -> int:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise NoDataAvailable("No data available!") from exc


class SimulatorTransport:
    """In-memory transport driving a :class:`~stiebel_heatpump.simulator.HeatPumpSimulator`."""

    def __init__(self, simulator: "HeatPumpSimulator") -> None:
        self._simulator = simulator
        self._rx: "queue.Queue[int]" = queue.Queue()

    @property
    def simulator(self) -> "HeatPumpSimulator":
        return self._simulator

    def open(self) -> None:  # nothing to do
        return None

    def close(self) -> None:  # nothing to do
        return None

    def write(self, data: bytes) -> None:
        for response_byte in self._simulator.feed(data):
            self._rx.put(response_byte)

    def get(self, timeout: float = 1.0) -> int:
        try:
            return self._rx.get(timeout=timeout)
        except queue.Empty as exc:
            raise NoDataAvailable("No data available!") from exc
