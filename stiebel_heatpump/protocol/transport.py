"""Byte transports for talking to the heat pump.

The rest of the protocol stack only depends on the small :class:`Transport`
interface -- ``write`` bytes and ``get`` one byte at a time -- so it works
identically against a real serial port and against the in-memory simulator used
for tests and offline demos.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from .parser import ProtocolError

if TYPE_CHECKING:  # pragma: no cover
    from ..simulator import HeatPumpSimulator

logger = logging.getLogger(__name__)


class NoDataAvailable(Exception):
    """Raised by :meth:`Transport.get` when no byte arrives in time."""


class TransportError(ProtocolError):
    """The byte link itself failed (port missing, unplugged, permission denied).

    It derives from :class:`ProtocolError` on purpose: the communication layer
    already retries and resets the link on that type, and the API already maps
    it to 503 -- a vanished adapter is "heat pump unreachable", not a 500.
    """


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

    Every :meth:`open` starts a *new* connection with its own queue, stop flag
    and reader thread, so bytes left over from a previous connection can never
    leak into the next conversation, and a reader that is still winding down
    cannot touch the new queue. Any I/O failure is reported as
    :class:`TransportError`; the owner is expected to :meth:`close` and
    :meth:`open` again (the communication layer does this on its retry path).
    """

    def __init__(self, port: str, baudrate: int = 9600) -> None:
        self._port = port
        self._baudrate = baudrate
        self._serial = None
        self._queue: "queue.Queue[int]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._failure: Optional[str] = None

    def open(self) -> None:
        import serial  # imported lazily so the package works without hardware deps

        self.close()  # never leak a half-open previous connection
        self._queue = queue.Queue()
        self._failure = None
        try:
            port = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.05,
            )
        except PermissionError as exc:
            raise TransportError(
                f"Permission denied opening serial port {self._port}: the service user must be "
                f"allowed to use the device (e.g. member of the 'dialout' group)"
            ) from exc
        except (serial.SerialException, OSError, ValueError) as exc:
            raise TransportError(f"Could not open serial port {self._port}: {exc}") from exc

        try:
            port.reset_input_buffer()
        except (serial.SerialException, OSError):  # best effort
            pass
        self._serial = port
        self._stop = threading.Event()
        self._reader = threading.Thread(
            target=self._read_loop, args=(port, self._queue, self._stop), daemon=True
        )
        self._reader.start()

    def _read_loop(self, port, rx: "queue.Queue[int]", stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                chunk = port.read(1)
            except Exception as exc:  # noqa: BLE001 - adapter unplugged, fd closed, ...
                if not stop.is_set():
                    self._failure = f"serial read failed: {exc}"
                    logger.warning("Serial port %s: %s", self._port, self._failure)
                return
            if chunk:
                rx.put(chunk[0])
            else:
                time.sleep(0.003)

    def close(self) -> None:
        self._stop.set()
        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        port, self._serial = self._serial, None
        if port is not None:
            try:
                port.close()
            except Exception:  # noqa: BLE001 - closing a vanished device may fail
                logger.debug("Ignoring error while closing serial port", exc_info=True)

    def write(self, data: bytes) -> None:
        port = self._serial
        if port is None:
            raise TransportError("serial port is not open")
        if self._failure:
            raise TransportError(self._failure)
        try:
            port.write(data)
            port.flush()
        except Exception as exc:  # noqa: BLE001
            self._failure = f"serial write failed: {exc}"
            raise TransportError(self._failure) from exc

    def get(self, timeout: float = 1.0) -> int:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty as exc:
            if self._failure:
                raise TransportError(self._failure) from exc
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
