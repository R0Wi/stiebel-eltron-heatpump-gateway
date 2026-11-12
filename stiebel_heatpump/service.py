"""Thread-safe facade over a single heat pump connection.

The serial link allows only one conversation at a time. Access to it goes
through an :class:`_IoGate`: one frame at a time, and pending writes go before
reads. With polling enabled (``poll=PollConfig(...)``) a background
:class:`~stiebel_heatpump.poller.Poller` keeps a per-frame cache fresh and API
reads are answered from it; without polling every read is a live round trip.
This is the object the REST layer talks to.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator, Optional, TypeVar

from .config_loader import HeatPumpConfig, Request
from .models import ChannelDefinition, ChannelValue, DataType, ValueKind
from .poller import FrameKey, Poller, frame_key
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


@dataclass(frozen=True)
class PollConfig:
    """Background polling parameters (seconds)."""

    refresh_s: float = 60
    settings_refresh_s: float = 900
    stale_after_s: float = 300


@dataclass(frozen=True)
class _CacheEntry:
    values: dict[str, object]
    updated_at: datetime
    stored_at: float  # monotonic, for age checks
    error: Optional[str] = None  # set when the latest poll of this frame failed


class _IoGate:
    """Mutual exclusion for the serial line: writers first, otherwise FIFO.

    Waiters queue up by ``(priority, arrival)``; the head of the queue gets the
    line when it is free. Writers have the better priority, so a write waits only
    for the conversation in progress. Order *within* a class is strictly first
    come, first served -- essential for the poller, which re-takes the gate the
    moment it releases it: without a queue it would starve every API read-through
    for a whole sweep. Callers hold the gate for a single frame.
    """

    _WRITER, _READER = 0, 1

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._busy = False
        self._waiting: list[tuple[int, int]] = []  # heap of (priority, arrival)
        self._arrivals = itertools.count()

    @contextmanager
    def reader(self) -> Iterator[None]:
        with self._hold(self._READER):
            yield

    @contextmanager
    def writer(self) -> Iterator[None]:
        with self._hold(self._WRITER):
            yield

    @contextmanager
    def _hold(self, priority: int) -> Iterator[None]:
        ticket = (priority, next(self._arrivals))
        with self._cond:
            heapq.heappush(self._waiting, ticket)
            try:
                while self._busy or self._waiting[0] != ticket:
                    self._cond.wait()
            except BaseException:
                self._waiting.remove(ticket)
                heapq.heapify(self._waiting)
                self._cond.notify_all()
                raise
            heapq.heappop(self._waiting)
            self._busy = True
        try:
            yield
        finally:
            with self._cond:
                self._busy = False
                self._cond.notify_all()


class HeatPumpService:
    """High level, thread-safe operations on a heat pump."""

    def __init__(
        self,
        config: HeatPumpConfig,
        transport: Transport,
        waiting_time_ms: int = 1200,
        poll: Optional[PollConfig] = None,
    ) -> None:
        self._config = config
        self._communication = CommunicationService(Connector(transport), waiting_time_ms)
        self._gate = _IoGate()
        self._poll = poll
        self._poller: Optional[Poller] = None
        self._cache: dict[FrameKey, _CacheEntry] = {}
        self._cache_lock = threading.Lock()
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
        poll = (
            PollConfig(settings.refresh_s, settings.settings_refresh_s, settings.stale_after_s)
            if settings.poll_enabled
            else None
        )
        return cls(config, transport, settings.waiting_time_ms, poll)

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Try to open the link now. Best effort: if the device is absent the
        service stays up and every later request retries the connection."""
        with self._gate.writer():
            try:
                self._communication.connect()
            except ProtocolError as exc:
                self._note_error(exc)
                logger.warning("Could not open the heat pump link yet (will retry on demand): %s", exc)

    def start_polling(self) -> None:
        """Start the background poller (no-op unless constructed with ``poll=``)."""
        if self._poll is None or self._poller is not None:
            return
        self._poller = Poller(
            self, self._config.requests, self._poll.refresh_s, self._poll.settings_refresh_s
        )
        self._poller.start()

    def close(self) -> None:
        poller, self._poller = self._poller, None
        if poller is not None:
            poller.stop()
        with self._gate.writer():
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

    # -- cache / frame access -------------------------------------------------

    def _read_live(self, request: Request, reuse_cached: bool = False) -> _CacheEntry:
        """A serial read on behalf of an API call.

        ``reuse_cached`` re-checks the cache once this call holds the line: the
        poller may have read the frame while we queued, and reading it again
        would only cost another paced round trip.
        """
        with self._gate.reader():
            if reuse_cached:
                with self._cache_lock:
                    entry = self._cache.get(frame_key(request))
                if entry is not None and entry.error is None:
                    return entry
            data = self._io(self._communication.read_request, request)
        return self._store(request, data)

    def _store(self, request: Request, data: dict[str, object], by_poller: bool = False) -> _CacheEntry:
        entry = _CacheEntry(dict(data), datetime.now(timezone.utc), time.monotonic())
        if self._poll is not None:
            with self._cache_lock:
                self._cache[frame_key(request)] = entry
            if not by_poller and self._poller is not None:
                self._poller.reschedule(frame_key(request))  # just read: not due again soon
        return entry

    def _frame(self, request: Request, fresh: bool = False) -> _CacheEntry:
        """The values of one request frame: from the cache if polling is on and
        the entry is usable, otherwise from a live read."""
        if self._poll is None or fresh:
            return self._read_live(request)
        with self._cache_lock:
            entry = self._cache.get(frame_key(request))
        if entry is None:  # not polled yet (just started)
            return self._read_live(request, reuse_cached=True)
        if entry.error is not None and time.monotonic() - entry.stored_at > self._poll.stale_after_s:
            raise ProtocolError(
                f"No fresh data for request {request.request_byte}: polling has been failing "
                f"since {entry.updated_at.isoformat()} ({entry.error})"
            )
        return entry

    def poll_frame(self, request: Request) -> bool:
        """One background poll of a frame. Called by the poller; never raises.

        A failure keeps the previous values in the cache but flags them, so
        they are served until ``stale_after_s`` and refused after that.
        """
        try:
            with self._gate.reader():
                data = self._io(self._communication.read_request, request)
        except Exception as exc:  # noqa: BLE001 - includes ProtocolError; poller must go on
            if not isinstance(exc, ProtocolError):
                logger.exception("Unexpected error while polling %s", request.request_byte)
            with self._cache_lock:
                entry = self._cache.get(frame_key(request))
                if entry is not None:
                    self._cache[frame_key(request)] = replace(entry, error=str(exc))
            return False
        self._store(request, data, by_poller=True)
        return True

    def _mark_due(self, keys: Iterable[FrameKey]) -> None:
        if self._poller is not None:
            for key in keys:
                self._poller.mark_due(key)

    # -- reads ---------------------------------------------------------------

    def get_version(self) -> str:
        request = self._config.request_by_bytes("FD")
        if request is None:
            return "<UNKNOWN_VERSION>"
        version = self._frame(request).values.get("version")
        if version is None:
            logger.warning("Version key not found in response data!")
            return "<UNKNOWN_VERSION>"
        return str(version)

    def read_channel(self, channel_id: str, fresh: bool = False) -> ChannelValue:
        record = self._require_channel(channel_id)
        request = self._config.request_for_channel(channel_id)
        entry = self._frame(request, fresh)
        if channel_id not in entry.values:
            raise ChannelNotFound(channel_id)
        return self._to_channel_value(record, entry.values[channel_id], entry.updated_at)

    def read_channels(self, channel_ids: Iterable[str], fresh: bool = False) -> list[ChannelValue]:
        """Read several channels at once.

        Raises :class:`ChannelNotFound` if *any* requested id is unknown, rather
        than silently returning a shorter list than the caller asked for.
        """
        wanted = list(channel_ids)
        unknown = [cid for cid in wanted if self._config.channel(cid) is None]
        if unknown:
            raise ChannelNotFound(", ".join(unknown))
        return self._collect(self._requests_for(wanted), fresh, set(wanted))

    def read_all(
        self, data_types: Optional[set[DataType]] = None, fresh: bool = False
    ) -> list[ChannelValue]:
        types = data_types or {DataType.SENSOR, DataType.STATUS}
        requests = [
            request
            for request in self._config.requests
            if any(record.data_type in types for record in request.records)
        ]
        return self._collect(requests, fresh, None, types)

    def _collect(
        self,
        requests: list[Request],
        fresh: bool,
        wanted: Optional[set[str]],
        types: Optional[set[DataType]] = None,
    ) -> list[ChannelValue]:
        results: list[ChannelValue] = []
        for request in requests:
            entry = self._frame(request, fresh)
            for channel_id, value in entry.values.items():
                record = self._config.channel(channel_id)
                if record is None:
                    continue
                if wanted is not None and channel_id not in wanted:
                    continue
                if types is not None and record.data_type not in types:
                    continue
                results.append(self._to_channel_value(record, value, entry.updated_at))
        return results

    # -- writes --------------------------------------------------------------

    def write_channel(self, channel_id: str, value: object) -> ChannelValue:
        record = self._require_channel(channel_id)
        if not record.writable:
            raise ChannelNotWritable(channel_id)
        coerced = self._coerce(record, value)
        # Writers jump ahead of polling: at most the frame in flight is waited for.
        with self._gate.writer():
            data = self._io(self._communication.write_data, coerced, record)
        result = data.get(channel_id, coerced)
        self._after_write(data, [record.request_key])
        return self._to_channel_value(record, result, datetime.now(timezone.utc))

    def set_time(self) -> dict[str, object]:
        request = self._config.request_by_bytes("FC")
        clock_records = {}
        for field, channel_id in _CLOCK_REGISTERS.items():
            record = self._config.channel(channel_id)
            if record is not None:
                clock_records[field] = record
        with self._gate.writer():
            result = self._io(self._communication.set_time, request, clock_records)
        keys = [record.request_key for record in clock_records.values()]
        if request is not None:
            keys.append(frame_key(request))
        self._drop_cached(keys)
        self._mark_due(keys)
        return result

    def _after_write(self, written: dict[str, object], keys: list[FrameKey]) -> None:
        """Make the confirmed value visible at once, then re-read the frame."""
        if self._poll is not None:
            now = datetime.now(timezone.utc)
            with self._cache_lock:
                for channel_id, value in written.items():
                    record = self._config.channel(channel_id)
                    entry = self._cache.get(record.request_key) if record else None
                    if entry is not None:
                        self._cache[record.request_key] = _CacheEntry(
                            {**entry.values, channel_id: value}, now, time.monotonic()
                        )
        self._mark_due(keys)

    def _drop_cached(self, keys: Iterable[FrameKey]) -> None:
        with self._cache_lock:
            for key in keys:
                self._cache.pop(key, None)

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
    def _to_channel_value(
        record: ChannelDefinition, value: object, updated_at: Optional[datetime] = None
    ) -> ChannelValue:
        return ChannelValue(
            channel_id=record.channel_id,
            value=value,
            unit=record.unit,
            data_type=record.data_type,
            writable=record.writable,
            updated_at=updated_at,
        )
