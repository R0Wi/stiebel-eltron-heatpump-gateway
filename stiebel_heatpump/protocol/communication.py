"""High level read/write operations, ported from ``CommunicationServiceImpl``.

This layer turns :class:`~stiebel_heatpump.config_loader.Request` objects into
serial round trips and decodes the responses into ``{channel_id: value}`` maps.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from ..config_loader import Request
from ..models import ChannelDefinition
from . import parser
from .connector import Connector

logger = logging.getLogger(__name__)

MAX_TRIES = 3

# The clock fields that are actually written, in order. Following FHEM
# (00_THZ.pm), the device derives the weekday from the date and has no settable
# "seconds" register, so neither is written -- only these five fields are.
_CLOCK_FIELDS = ("day", "month", "year", "hours", "minutes")


class CommunicationService:
    """Read and write heat pump values over a :class:`Connector`."""

    def __init__(self, connector: Connector, waiting_time_ms: int = 1200) -> None:
        self._connector = connector
        self._waiting_time = waiting_time_ms / 1000.0

    def connect(self) -> None:
        self._connector.connect()

    def close(self) -> None:
        self._connector.disconnect()

    # -- reading -------------------------------------------------------------

    def get_version(self, version_request: Request) -> str:
        data = self.read_request(version_request)
        version = data.get("version")
        if version is None:
            logger.warning("Version key not found in response data!")
            return "<UNKNOWN_VERSION>"
        return str(version)

    def read_request(self, request: Request) -> dict[str, object]:
        """Read a single request (with retries + header validation).

        Raises :class:`~stiebel_heatpump.protocol.parser.ProtocolError` if the
        device cannot be read after ``MAX_TRIES`` attempts. Callers must not
        treat "no data" as "the channel doesn't exist" -- an unreachable
        device and an unknown channel id are different failure modes and are
        surfaced differently by the service/API layers.
        """
        last_error: Optional[str] = None
        for attempt in range(1, MAX_TRIES + 1):
            try:
                response = self._connector.get_data(
                    parser.create_request_message(request.request_bytes)
                )
                if not parser.header_check(response):
                    raise parser.ProtocolError(
                        f"invalid or missing response: {parser.bytes_to_hex(response)}"
                    )
                if request.request_bytes2 is None:
                    return parser.parse_records(response, request.records)
                time.sleep(self._waiting_time)
                response2 = self._connector.get_data(
                    parser.create_request_message(request.request_bytes2)
                )
                if not parser.header_check(response2):
                    raise parser.ProtocolError(
                        f"invalid or missing response2: {parser.bytes_to_hex(response2)}"
                    )
                return parser.parse_records(response, request.records, response2)
            except parser.ProtocolError as exc:
                last_error = str(exc)
                logger.warning("Error reading data for %s: %s (retry %s)",
                               request.request_byte, exc, attempt)
                self._restart()
        raise parser.ProtocolError(
            f"Could not read data for request {request.request_byte} after "
            f"{MAX_TRIES} attempts: {last_error}"
        )

    def read_requests(self, requests: list[Request]) -> dict[str, object]:
        """Read a list of requests, pausing ``waiting_time`` between them."""
        data: dict[str, object] = {}
        for index, request in enumerate(requests):
            data.update(self.read_request(request))
            if len(requests) > 1 and index < len(requests) - 1:
                time.sleep(self._waiting_time)
        return data

    # -- writing -------------------------------------------------------------

    def write_data(self, new_value: object, record: ChannelDefinition) -> dict[str, object]:
        return self._write_values([(new_value, record)])

    def write_time_quarter_pair(
        self,
        start_value: object,
        end_value: object,
        record_start: ChannelDefinition,
        record_end: ChannelDefinition,
    ) -> dict[str, object]:
        return self._write_values([(start_value, record_start), (end_value, record_end)])

    def _write_values(self, items: list[tuple[object, ChannelDefinition]]) -> dict[str, object]:
        first_record = items[0][1]
        channel_ids = ";".join(record.channel_id for _, record in items)

        read_request_message = parser.create_request_message(bytes.fromhex(first_record.request_byte))
        read_response = self._connector.get_data(read_request_message)

        if read_request_message == read_response:
            logger.debug("Current value(s) for %s already set.", channel_ids)
            return {}
        if not read_response:
            # We could not even read the current state, so no write was
            # attempted at all. This must not be reported as success with the
            # caller's requested value substituted in -- raise instead of
            # returning {}, so the caller can tell "write failed" apart from
            # "value already matched, nothing to do".
            raise parser.ProtocolError(
                f"Could not read current value(s) for {channel_ids} before writing; "
                "write was not attempted."
            )

        update = bytearray(read_response)
        for value, record in items:
            parser.compose_record(value, update, record)

        time.sleep(self._waiting_time)
        # Escape the composed frame before sending, exactly like the read path
        # (create_request_message) and FHEM's THZ_encodecommand -- otherwise a
        # 0x10 or 0x2B byte in the payload corrupts the frame on the wire.
        set_response = self._connector.set_data(parser.add_duplicated_bytes(bytes(update)))

        try:
            parser.verify_header(set_response)
        except parser.ProtocolError as exc:
            # The device did not confirm the SET, so it still holds the old
            # value. The binding only logged a warning here and reported the
            # unchanged value as if the call had succeeded; for a REST API that
            # turns a rejected write into a 200, so raise instead. The device's
            # own error text (from verify_header) and the value it kept are
            # both included, so the caller learns why the write was refused.
            unchanged = {
                record.channel_id: parser.parse_record(read_response, record)
                for _, record in items
            }
            raise parser.WriteNotConfirmed(
                f"Heat pump did not confirm the write of {channel_ids} ({exc}); "
                f"the device still reports {unchanged}."
            ) from exc

        logger.debug("Updated parameter %s successfully.", channel_ids)
        return {
            record.channel_id: parser.parse_record(bytes(update), record)
            for _, record in items
        }

    # -- time synchronisation ------------------------------------------------

    def set_time(
        self,
        read_request: Optional[Request],
        clock_records: Optional[dict[str, ChannelDefinition]] = None,
    ) -> dict[str, object]:
        """Set the heat pump clock to the current system time.

        ``clock_records`` maps each clock field (``day``, ``month``, ``year``,
        ``hours``, ``minutes``) to the :class:`ChannelDefinition` of its
        individual writable register (commands ``0A0122``..``0A0126``). This is
        how firmware 4.39/5.39/7.x sets the clock in FHEM (``%sets439539common``
        in ``00_THZ.pm``): every register is written on its own, with a
        read-modify-write round trip. The read-only ``FC`` date/time register
        used to *display* the clock cannot be written back -- which is why the
        original whole-``FC``-frame write never worked.

        When the device definition does not expose those registers (older 2.x
        firmware, where FHEM writes the date/time fields straight into the ``FC``
        register), the values are composed into ``read_request`` in a single
        write instead.

        ``read_request`` (the ``FC`` request) is finally read back so the
        response reports the resulting device time.
        """
        now = datetime.now()
        values: dict[str, object] = {
            "day": now.day,
            "month": now.month,
            "year": now.year % 100,
            "hours": now.hour,
            "minutes": now.minute,
        }

        if clock_records:
            written = self._set_time_via_registers(clock_records, values)
        elif read_request is not None:
            written = self._set_time_via_frame(read_request, values)
        else:
            logger.warning("No clock registers available; skip setting time.")
            return {}

        data: dict[str, object] = {}
        if read_request is not None:
            response = self._connector.get_data(
                parser.create_request_message(read_request.request_bytes)
            )
            if parser.header_check(response):
                data = parser.parse_records(response, read_request.records)
        data.update(written)
        data["lastUpdate"] = now.strftime("%Y-%m-%d %H:%M:%S")
        return data

    def _set_time_via_registers(
        self, clock_records: dict[str, ChannelDefinition], values: dict[str, object]
    ) -> dict[str, object]:
        """Write each clock field to its own register (firmware 4.39/5.39/7.x)."""
        written: dict[str, object] = {}
        for field in _CLOCK_FIELDS:
            record = clock_records.get(field)
            if record is None:
                continue
            written.update(self._write_values([(values[field], record)]))
            time.sleep(self._waiting_time)
        return written

    def _set_time_via_frame(
        self, read_request: Request, values: dict[str, object]
    ) -> dict[str, object]:
        """Compose the date/time fields into the FC register (older 2.x firmware)."""
        response = self._connector.get_data(
            parser.create_request_message(read_request.request_bytes)
        )
        if not parser.header_check(response):
            logger.warning("Could not read current time from heat pump.")
            return {}

        update = bytearray(response)
        written: dict[str, object] = {}
        for record in read_request.records:
            if record.channel_id in values:
                parser.compose_record(values[record.channel_id], update, record)
                written[record.channel_id] = values[record.channel_id]

        time.sleep(self._waiting_time)
        self._connector.set_data(parser.add_duplicated_bytes(bytes(update)))
        time.sleep(self._waiting_time)
        return written

    # -- helpers -------------------------------------------------------------

    def _restart(self) -> None:
        logger.debug("Restarting connector")
        self._connector.disconnect()
        time.sleep(0.5)
        self._connector.connect()
