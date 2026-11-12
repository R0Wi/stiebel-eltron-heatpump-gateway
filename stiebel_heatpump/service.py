"""Thread-safe facade over a single heat pump connection.

The serial link allows only one conversation at a time, so every operation is
guarded by a lock. This is the object the REST layer talks to.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional, TypeVar

from .config_loader import HeatPumpConfig, Request
from .models import ChannelDefinition, ChannelValue, DataType, ValueKind
from .protocol.communication import CommunicationService
from .protocol.connector import Connector
from .protocol.parser import ProtocolError, WriteNotConfirmed
from .protocol.transport import SerialTransport, SimulatorTransport, Transport
from .settings import AppSettings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Clock field -> channel id of the individual writable register used to set the
# heat pump clock on firmware 4.39/5.39/7.x (see the device configs and FHEM's
# %sets439539common). Absent on older 2.x definitions, which fall back to
# writing the FC register directly.
_CLOCK_REGISTERS = {
    "day": "pClockDay",
    "month": "pClockMonth",
    "year": "pClockYear",
    "hours": "pClockHour",
    "minutes": "pClockMinutes",
}


class ChannelNotFound(KeyError):
    """Raised when an unknown channel id is requested."""


class ChannelNotWritable(ValueError):
    """Raised when a write is attempted against a non-settings channel."""


class HeatPumpService:
    """High level, thread-safe operations on a heat pump."""

    def __init__(self, config: HeatPumpConfig, transport: Transport, waiting_time_ms: int = 1200) -> None:
        self._config = config
        self._communication = CommunicationService(Connector(transport), waiting_time_ms)
        self._lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._last_success: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_error_at: Optional[datetime] = None

    # -- construction --------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: AppSettings, config: HeatPumpConfig) -> "HeatPumpService":
        if settings.transport == "serial":
            transport: Transport = SerialTransport(settings.port, settings.baud_rate)
        else:
            from .simulator import HeatPumpSimulator

            transport = SimulatorTransport(HeatPumpSimulator(config))
        return cls(config, transport, settings.waiting_time_ms)

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Try to open the link now. Best effort: if the device is absent the
        service stays up and every later request retries the connection."""
        with self._lock:
            try:
                self._communication.connect()
            except ProtocolError as exc:
                self._note_error(exc)
                logger.warning("Could not open the heat pump link yet (will retry on demand): %s", exc)

    def close(self) -> None:
        with self._lock:
            self._communication.close()

    # -- link health ---------------------------------------------------------

    def _note_success(self) -> None:
        with self._status_lock:
            self._last_success = datetime.now(timezone.utc)

    def _note_error(self, exc: Exception) -> None:
        with self._status_lock:
            self._last_error = str(exc)
            self._last_error_at = datetime.now(timezone.utc)

    def _io(self, operation: Callable[..., T], *args: object) -> T:
        """Run one serial conversation and record whether the link worked."""
        try:
            result = operation(*args)
        except WriteNotConfirmed:
            self._note_success()  # the device answered; it just refused the SET
            raise
        except ProtocolError as exc:
            self._note_error(exc)
            raise
        self._note_success()
        return result

    def link_status(self) -> dict[str, object]:
        """``up`` once the latest conversation succeeded, ``down`` once the latest
        failed, ``unknown`` before the first one."""
        with self._status_lock:
            if self._last_success is None and self._last_error_at is None:
                state = "unknown"
            elif self._last_error_at is None or (
                self._last_success is not None and self._last_success >= self._last_error_at
            ):
                state = "up"
            else:
                state = "down"
            return {
                "link": state,
                "last_success": self._last_success,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
            }

    @property
    def config(self) -> HeatPumpConfig:
        return self._config

    # -- reads ---------------------------------------------------------------

    def get_version(self) -> str:
        request = self._config.request_by_bytes("FD")
        if request is None:
            return "<UNKNOWN_VERSION>"
        with self._lock:
            return self._io(self._communication.get_version, request)

    def read_channel(self, channel_id: str) -> ChannelValue:
        record = self._require_channel(channel_id)
        request = self._config.request_for_channel(channel_id)
        with self._lock:
            data = self._io(self._communication.read_request, request)
        if channel_id not in data:
            raise ChannelNotFound(channel_id)
        return self._to_channel_value(record, data[channel_id])

    def read_channels(self, channel_ids: Iterable[str]) -> list[ChannelValue]:
        """Read several channels at once.

        Raises :class:`ChannelNotFound` if *any* requested id is unknown, rather
        than silently returning a shorter list than the caller asked for.
        """
        wanted = list(channel_ids)
        unknown = [cid for cid in wanted if self._config.channel(cid) is None]
        if unknown:
            raise ChannelNotFound(", ".join(unknown))
        requests = self._requests_for(wanted)
        wanted_set = set(wanted)
        results: list[ChannelValue] = []
        with self._lock:
            data = self._io(self._communication.read_requests, requests)
        for channel_id, value in data.items():
            if channel_id in wanted_set:
                record = self._config.channel(channel_id)
                if record is not None:
                    results.append(self._to_channel_value(record, value))
        return results

    def read_all(self, data_types: Optional[set[DataType]] = None) -> list[ChannelValue]:
        types = data_types or {DataType.SENSOR, DataType.STATUS}
        requests = [
            request
            for request in self._config.requests
            if any(record.data_type in types for record in request.records)
        ]
        with self._lock:
            data = self._io(self._communication.read_requests, requests)
        results: list[ChannelValue] = []
        for channel_id, value in data.items():
            record = self._config.channel(channel_id)
            if record is not None and record.data_type in types:
                results.append(self._to_channel_value(record, value))
        return results

    # -- writes --------------------------------------------------------------

    def write_channel(self, channel_id: str, value: object) -> ChannelValue:
        record = self._require_channel(channel_id)
        if not record.writable:
            raise ChannelNotWritable(channel_id)
        coerced = self._coerce(record, value)
        with self._lock:
            data = self._io(self._communication.write_data, coerced, record)
        result = data.get(channel_id, coerced)
        return self._to_channel_value(record, result)

    def set_time(self) -> dict[str, object]:
        request = self._config.request_by_bytes("FC")
        clock_records = {}
        for field, channel_id in _CLOCK_REGISTERS.items():
            record = self._config.channel(channel_id)
            if record is not None:
                clock_records[field] = record
        with self._lock:
            return self._io(self._communication.set_time, request, clock_records)

    # -- helpers -------------------------------------------------------------

    def _require_channel(self, channel_id: str) -> ChannelDefinition:
        record = self._config.channel(channel_id)
        if record is None:
            raise ChannelNotFound(channel_id)
        return record

    def _requests_for(self, channel_ids: list[str]) -> list[Request]:
        requests: list[Request] = []
        for channel_id in channel_ids:
            request = self._config.request_for_channel(channel_id)
            if request is not None and request not in requests:
                requests.append(request)
        return requests

    _TRUE_STRINGS = frozenset({"1", "true", "on", "yes"})
    _FALSE_STRINGS = frozenset({"0", "false", "off", "no"})

    @classmethod
    def _coerce(cls, record: ChannelDefinition, value: object) -> object:
        kind = record.value_kind
        if kind == ValueKind.BOOLEAN:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in cls._TRUE_STRINGS:
                    return True
                if normalized in cls._FALSE_STRINGS:
                    return False
                raise ValueError(
                    f"Invalid boolean value '{value}' for channel '{record.channel_id}'; "
                    f"expected one of {sorted(cls._TRUE_STRINGS | cls._FALSE_STRINGS)}"
                )
            return bool(value)
        if kind == ValueKind.NUMBER:
            return float(value)  # type: ignore[arg-type]
        return int(value)  # type: ignore[arg-type]

    @staticmethod
    def _to_channel_value(record: ChannelDefinition, value: object) -> ChannelValue:
        return ChannelValue(
            channel_id=record.channel_id,
            value=value,
            unit=record.unit,
            data_type=record.data_type,
            writable=record.writable,
        )
