"""Thread-safe facade over a single heat pump connection.

The serial link allows only one conversation at a time, so every operation is
guarded by a lock. This is the object the REST layer talks to.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, Optional

from .config_loader import HeatPumpConfig, Request
from .models import ChannelDefinition, ChannelValue, DataType, ValueKind
from .protocol.communication import CommunicationService
from .protocol.connector import Connector
from .protocol.transport import SerialTransport, SimulatorTransport, Transport
from .settings import AppSettings

logger = logging.getLogger(__name__)

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
        self._connected = False

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
        with self._lock:
            if not self._connected:
                self._communication.connect()
                self._connected = True

    def close(self) -> None:
        with self._lock:
            if self._connected:
                self._communication.close()
                self._connected = False

    @property
    def config(self) -> HeatPumpConfig:
        return self._config

    # -- reads ---------------------------------------------------------------

    def get_version(self) -> str:
        request = self._config.request_by_bytes("FD")
        if request is None:
            return "<UNKNOWN_VERSION>"
        with self._lock:
            return self._communication.get_version(request)

    def read_channel(self, channel_id: str) -> ChannelValue:
        record = self._require_channel(channel_id)
        request = self._config.request_for_channel(channel_id)
        with self._lock:
            data = self._communication.read_request(request)
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
            data = self._communication.read_requests(requests)
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
            data = self._communication.read_requests(requests)
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
            data = self._communication.write_data(coerced, record)
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
            return self._communication.set_time(request, clock_records)

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
