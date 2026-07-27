"""An in-memory heat pump simulator.

It speaks the same byte protocol as a real device, so the whole stack -- from
the connector up to the REST API -- can run and be tested without any hardware.
It keeps a response frame per request command, decodes SET frames to update its
state (making reads-after-writes consistent) and answers the start / data /
receive handshake.
"""

from __future__ import annotations

from typing import Optional

from .config_loader import HeatPumpConfig, Request
from .protocol import parser

_IDLE = "idle"
_COLLECTING = "collecting"
_AWAIT_ACK = "await_ack"


class HeatPumpSimulator:
    """A fake Stiebel Eltron heat pump driven purely by a config."""

    def __init__(self, config: HeatPumpConfig, seed_values: Optional[dict[str, object]] = None) -> None:
        self._config = config
        self._store: dict[str, bytearray] = {}
        # A request with request_byte2 set is read as two fully independent
        # commands, each with its own complete request/response round trip --
        # see CommunicationServiceImpl.readData() in the original binding:
        # `getData(createRequestMessage(request.getRequestByte2()))` is the
        # exact same call used for a primary command, just with different
        # command bytes. The original Java test suite confirms this with a
        # captured device response for a *secondary* command on its own,
        # e.g. "0100300A091B00011003" for command 0A091B (ported verbatim into
        # test_read_request in tests/test_communication.py) -- a complete,
        # well-formed, independently-addressable response frame. Both commands
        # therefore need their own stored response frame here, built from the
        # same record positions/lengths (parse_records() decodes response and
        # response2 with the identical RecordDefinition list and combines them,
        # see DataParser.parseRecords: `value2 * 1000 + valueShort`).
        self._command_to_request: dict[str, Request] = {}
        for request in config.requests:
            self._store[request.request_byte] = self._build_frame(request, request.request_bytes)
            self._command_to_request[request.request_byte] = request
            if request.request_byte2:
                self._store[request.request_byte2] = self._build_frame(request, request.request_bytes2)
                self._command_to_request[request.request_byte2] = request

        seeds = dict(seed_values or {})
        seeds.setdefault("version", self._default_version())
        for channel_id, value in seeds.items():
            self.set_value(channel_id, value)

        self._state = _IDLE
        self._frame_buffer = bytearray()
        self._pending: bytes = b""

    # -- seeding -------------------------------------------------------------

    def _default_version(self) -> float:
        name = self._config.name or ""
        digits = [part for part in name.replace("-", "_").split("_") if part.isdigit()]
        if len(digits) >= 2:
            return float(f"{digits[-2]}.{digits[-1]}")
        return 7.59

    def _build_frame(self, request: Request, command_bytes: bytes) -> bytearray:
        data_end = max((r.position + r.length for r in request.records), default=5)
        frame = bytearray(data_end + 2)
        frame[0] = parser.HEADER_START
        frame[1] = parser.GET
        frame[3 : 3 + len(command_bytes)] = command_bytes
        frame[-2] = parser.ESCAPE
        frame[-1] = parser.END
        frame[2] = parser.calculate_checksum(frame)
        return frame

    def set_value(self, channel_id: str, value: object) -> None:
        """Seed/override the value a channel will report on the next read.

        For a channel whose request spans two independent commands
        (``request_byte`` / ``request_byte2``), ``value`` is split the same
        way ``parse_records()`` recombines them on read: the primary command's
        frame carries ``value % 1000`` and the secondary command's frame
        carries ``value // 1000`` -- see ``DataParser.parseRecords`` in the
        original binding (``value2 * 1000 + valueShort``), reproduced in
        ``protocol.parser.parse_records``.
        """
        record = self._config.channel(channel_id)
        if record is None:
            return
        request = self._config.request_for_channel(channel_id)
        if request is None:
            return

        frame = self._store[request.request_byte]
        if request.request_byte2 and not isinstance(value, bool):
            low, high = int(value) % 1000, int(value) // 1000
            self._encode(frame, record, low)
            frame2 = self._store[request.request_byte2]
            self._encode(frame2, record, high)
            frame2[1] = parser.GET
            frame2[2] = parser.calculate_checksum(frame2)
        else:
            self._encode(frame, record, value)

        frame[1] = parser.GET
        frame[2] = parser.calculate_checksum(frame)

    @staticmethod
    def _encode(frame: bytearray, record, value: object) -> None:
        if isinstance(value, bool):
            if record.bit_position > 0:
                frame[record.position] = parser._set_bit(  # noqa: SLF001
                    frame[record.position], record.bit_position, value
                )
            else:
                frame[record.position] = 1 if value else 0
            return
        if isinstance(value, float):
            # Same rounding as the real write path (parser.compose_record), so
            # seeded values and written values cannot encode differently.
            short_value = parser.scaled_to_raw(value, record.scale)
        else:
            short_value = int(value)
        encoded = parser.short_to_bytes(short_value)
        if record.length == 1:
            frame[record.position] = encoded[0]
        elif record.length == 2:
            frame[record.position] = encoded[1]
            frame[record.position + 1] = encoded[0]

    # -- protocol state machine ---------------------------------------------

    def feed(self, data: bytes) -> bytes:
        """Consume bytes written by the client; return the bytes to send back."""
        out = bytearray()
        for byte in data:
            out.extend(self._feed_byte(byte))
        return bytes(out)

    def _feed_byte(self, byte: int) -> bytes:
        if self._state == _IDLE:
            if byte == parser.START_COMMUNICATION:
                self._state = _COLLECTING
                self._frame_buffer = bytearray()
                return bytes([parser.ESCAPE])
            return b""

        if self._state == _COLLECTING:
            self._frame_buffer.append(byte)
            if len(self._frame_buffer) >= 6 and parser.is_frame_end(self._frame_buffer):
                self._pending = parser.fix_duplicated_bytes(bytes(self._frame_buffer))
                self._state = _AWAIT_ACK
                return bytes(parser.DATA_AVAILABLE)
            return b""

        if self._state == _AWAIT_ACK:
            if byte == parser.ESCAPE:
                response = self._build_response(self._pending)
                self._state = _IDLE
                return parser.add_duplicated_bytes(response)
            return b""

        return b""

    def _match_command(self, frame: bytes) -> Optional[str]:
        """Return the stored command key (hex) matching the sent frame, if any.

        Matches against every known command -- both primary ``request_byte``
        and secondary ``request_byte2`` values, since the device answers each
        as an independent command -- preferring the longest match to
        disambiguate overlapping prefixes.
        """
        best: Optional[str] = None
        for command_hex in self._command_to_request:
            command_bytes = bytes.fromhex(command_hex)
            if frame[3 : 3 + len(command_bytes)] == command_bytes:
                if best is None or len(command_bytes) > len(bytes.fromhex(best)):
                    best = command_hex
        return best

    def _build_response(self, frame: bytes) -> bytes:
        if len(frame) < 4:
            return bytes([parser.HEADER_START, parser.GET, 0x00, 0x00]) + parser.FOOTER
        command_hex = self._match_command(frame)
        get_or_set = frame[1]

        if command_hex is None:
            command = frame[3:4] if len(frame) > 4 else b"\x00"
            error = bytearray([parser.HEADER_START, 0x03, 0x00]) + command + parser.FOOTER
            error[2] = parser.calculate_checksum(error)
            return bytes(error)

        if get_or_set == parser.SET:
            # The incoming frame carries the new machine state; store it as GET.
            updated = bytearray(frame)
            updated[1] = parser.GET
            updated[2] = parser.calculate_checksum(updated)
            self._store[command_hex] = updated
            confirm = bytearray([parser.HEADER_START, parser.SET, 0x00]) + bytes.fromhex(command_hex) + parser.FOOTER
            confirm[2] = parser.calculate_checksum(confirm)
            return bytes(confirm)

        return bytes(self._store[command_hex])
