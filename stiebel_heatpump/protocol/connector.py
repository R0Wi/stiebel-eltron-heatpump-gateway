"""Low level request/response handshake with the heat pump.

Ported from the private ``getData`` / ``setData`` / ``startCommunication`` /
``establishRequest`` / ``receiveData`` methods of ``CommunicationServiceImpl``.

Handshake overview for a *read*::

    1. send 0x02 (start), expect 0x10 (ack)
    2. send the request frame, expect 0x10 0x02 (data available)
    3. send 0x10 (ack)
    4. receive bytes until the 0x10 0x03 footer, then de-escape
"""

from __future__ import annotations

import logging

from . import parser
from .transport import NoDataAvailable, Transport

logger = logging.getLogger(__name__)

MAX_RETRY = 5
INPUT_BUFFER_LENGTH = 1024


class Connector:
    """Drives a :class:`Transport` through the heat pump handshake."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def connect(self) -> None:
        self._transport.open()

    def disconnect(self) -> None:
        self._transport.close()

    # -- public read / write -------------------------------------------------

    def get_data(self, request_message: bytes) -> bytes:
        """Perform a full read handshake and return the de-escaped response frame."""
        self._start_communication()
        if not self._establish_request(request_message):
            return b""
        try:
            self._transport.write(bytes([parser.ESCAPE]))
            return self._receive_data()
        except Exception:  # noqa: BLE001
            logger.exception("Could not get data from heat pump!")
            return b""

    def set_data(self, request_message: bytes) -> bytes:
        """Perform a full write handshake and return the confirmation frame."""
        try:
            self._start_communication()
            self._establish_request(request_message)
            self._transport.write(bytes([parser.ESCAPE]))
        except Exception:  # noqa: BLE001
            logger.exception("Could not set data to heat pump!")
            return b""
        return self._receive_data()

    # -- handshake internals -------------------------------------------------

    def _start_communication(self) -> None:
        self._transport.write(bytes([parser.START_COMMUNICATION]))
        try:
            response = self._transport.get()
        except NoDataAvailable as exc:
            raise parser.ProtocolError(
                f"Heat pump communication could not be established! {exc}"
            ) from exc
        if response != parser.ESCAPE:
            raise parser.ProtocolError(
                "Heat pump is communicating, but did not receive Escape message in "
                f"initial handshake. Received: {response:02X}"
            )

    def _establish_request(self, request_message: bytes) -> bool:
        buffer = bytearray(INPUT_BUFFER_LENGTH)
        request_retry = 0
        while request_retry < MAX_RETRY:
            num_read = 0
            self._transport.write(request_message)
            buffer_retry = 0
            while buffer_retry < MAX_RETRY:
                try:
                    single = self._transport.get()
                except NoDataAvailable:
                    buffer_retry += 1
                    continue
                if num_read >= len(buffer):
                    # Bytes keep arriving but never signal "data available" --
                    # a noisy line or a stuck device, not a transient hiccup.
                    # Fail cleanly instead of overflowing the fixed buffer.
                    raise parser.ProtocolError(
                        "Received too much data while waiting for the "
                        "data-available signal; giving up."
                    )
                buffer[num_read] = single
                num_read += 1
                if buffer[0] != parser.DATA_AVAILABLE[0] or buffer[1] != parser.DATA_AVAILABLE[1]:
                    continue
                return True
            request_retry += 1
            self._start_communication()
        logger.warning("heat pump has no data available for request!")
        return False

    def _receive_data(self) -> bytes:
        buffer = bytearray(INPUT_BUFFER_LENGTH)
        num_read = 0
        retry = 0
        while retry < MAX_RETRY:
            try:
                single = self._transport.get()
            except NoDataAvailable:
                retry += 1
                continue
            if num_read >= len(buffer):
                # Same rationale as above: bytes keep coming but the footer
                # never shows up. Raise rather than overflow; get_data()'s
                # caller already treats a receive failure as "no data".
                raise parser.ProtocolError(
                    "Received too much data while waiting for the end-of-frame footer; giving up."
                )
            buffer[num_read] = single
            num_read += 1
            if num_read > 4 and parser.is_frame_end(buffer, num_read):
                break
        return parser.fix_duplicated_bytes(bytes(buffer[:num_read]))
