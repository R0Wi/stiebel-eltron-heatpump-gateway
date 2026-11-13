"""Background polling of the heat pump.

A daemon thread reads one request frame at a time and hands the result to the
service's cache, so API reads are answered from memory instead of waiting
``waiting_time_ms`` per frame on the serial line (a Settings sweep on a THZ 504
is ~35 s). This is the equivalent of the binding's scheduled refresh job.

Scheduling rules:

* every frame has its own due time; Sensor/Status frames come first, so a long
  Settings sweep can never delay the live values;
* only one frame is read per iteration, and each read goes through the
  service's I/O gate, which lets pending writes go first (like the binding's
  ``commandsPendingCounter``) -- a write waits for at most one frame;
* after a failed read the poller backs off, so an absent pump is not hammered.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable

from .config_loader import Request
from .models import DataType

if TYPE_CHECKING:  # pragma: no cover
    from .service import HeatPumpService

logger = logging.getLogger(__name__)

_MAX_BACKOFF_S = 30.0
_BACKOFF_STEP_S = 5.0

FrameKey = tuple[str, "str | None"]


def frame_key(request: Request) -> FrameKey:
    return (request.request_byte, request.request_byte2)


def is_settings_frame(request: Request) -> bool:
    """A frame is polled at the slow rate if it holds no Sensor/Status channel."""
    return not any(record.data_type != DataType.SETTINGS for record in request.records)


class Poller:
    def __init__(
        self,
        service: "HeatPumpService",
        requests: list[Request],
        refresh_s: float,
        settings_refresh_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._clock = clock
        self._frames: dict[FrameKey, tuple[Request, float, bool]] = {}
        for request in requests:
            slow = is_settings_frame(request)
            self._frames[frame_key(request)] = (request, settings_refresh_s if slow else refresh_s, slow)
        self._due: dict[FrameKey, float] = {key: 0.0 for key in self._frames}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="heatpump-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10.0)

    # -- scheduling ----------------------------------------------------------

    def mark_due(self, key: FrameKey) -> None:
        """Poll this frame as soon as the bus is free (e.g. after a write)."""
        with self._lock:
            if key in self._due:
                self._due[key] = 0.0
        self._wake.set()

    def reschedule(self, key: FrameKey) -> None:
        """The frame was just read by someone else: push its next poll out."""
        with self._lock:
            if key in self._frames:
                self._due[key] = self._clock() + self._frames[key][1]

    def _next(self) -> tuple["Request | None", float]:
        """The frame to read now, or how long (s) until one is due."""
        now = self._clock()
        with self._lock:
            due = [(slow, self._due[key], key) for key, (_, _, slow) in self._frames.items()
                   if self._due[key] <= now]
            if due:
                _, _, key = min(due)  # Sensor/Status (slow=False) first, then most overdue
                request, interval, _ = self._frames[key]
                # Reschedule *before* polling so a mark_due() that arrives
                # while the read is in flight is not overwritten afterwards.
                self._due[key] = now + interval
                return request, 0.0
            return None, max(0.0, min(self._due.values(), default=now + 1.0) - now)

    def _run(self) -> None:
        while not self._stop.is_set():
            request, wait = self._next()
            if request is None:
                self._wake.wait(timeout=wait)
                self._wake.clear()
                continue
            try:
                ok = self._service.poll_frame(request)
            except Exception:  # noqa: BLE001 - the poller must never die
                logger.exception("Unexpected error while polling %s", request.request_byte)
                ok = False
            if ok:
                self._failures = 0
            else:
                self._failures += 1
                self._stop.wait(min(_BACKOFF_STEP_S * self._failures, _MAX_BACKOFF_S))
